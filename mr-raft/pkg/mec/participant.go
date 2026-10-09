package mec

import (
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	sm "github.com/lni/dragonboat/v3/statemachine"
)

const txnRecordPrefix = "\x00txn\x00"
const txnLockPrefix = "\x00txn-lock\x00"

// participantStorage is accessed while the state machine's update lock is held.
// Disk implementations stage every mutation in the same indexed Pebble batch.
type participantStorage interface {
	transaction(string) (TxnRecord, bool, error)
	setTransaction(TxnRecord) error
	lockOwner(string) (string, error)
	setLock(string, string) error
	version(string) (uint64, error)
	write(KVData) error
}

func validUserKey(key string) bool {
	return key != "" && key != appliedIndexKey && !strings.HasPrefix(key, "\x00")
}

func canonicalTransaction(t Transaction, region uint64) (Transaction, error) {
	if t.ID == "" || t.Coordinator == "" || !validUserKey(t.Write.Key) || len(t.Participants) == 0 {
		return Transaction{}, errors.New("transaction requires ID, coordinator, participants and a valid user key")
	}
	t.Participants = append([]uint64(nil), t.Participants...)
	sort.Slice(t.Participants, func(i, j int) bool { return t.Participants[i] < t.Participants[j] })
	containsRegion := false
	for i, p := range t.Participants {
		if p == 0 || (i > 0 && p == t.Participants[i-1]) {
			return Transaction{}, errors.New("invalid or duplicate transaction participant")
		}
		containsRegion = containsRegion || p == region
	}
	if !containsRegion {
		return Transaction{}, errors.New("region is not a transaction participant")
	}
	return t, nil
}

func sameTransaction(a, b Transaction) bool {
	if a.ID != b.ID || a.Coordinator != b.Coordinator || a.Write != b.Write || len(a.Participants) != len(b.Participants) {
		return false
	}
	a.Participants = append([]uint64(nil), a.Participants...)
	b.Participants = append([]uint64(nil), b.Participants...)
	sort.Slice(a.Participants, func(i, j int) bool { return a.Participants[i] < a.Participants[j] })
	sort.Slice(b.Participants, func(i, j int) bool { return b.Participants[i] < b.Participants[j] })
	for i := range a.Participants {
		if a.Participants[i] != b.Participants[i] {
			return false
		}
	}
	return true
}

func copyTxnRecord(r TxnRecord) TxnRecord {
	r.Transaction.Participants = append([]uint64(nil), r.Transaction.Participants...)
	return r
}

func decodeReplicatedCommand(data []byte) (*TxnCommand, *KVData, error) {
	var envelope map[string]json.RawMessage
	if err := json.Unmarshal(data, &envelope); err != nil {
		return nil, nil, err
	}
	if _, ok := envelope["txn_op"]; ok {
		var command TxnCommand
		if err := json.Unmarshal(data, &command); err != nil {
			return nil, nil, err
		}
		return &command, nil, nil
	}
	var kv KVData
	if err := json.Unmarshal(data, &kv); err != nil {
		return nil, nil, err
	}
	return nil, &kv, nil
}

func rejectTxn(message string) sm.Result { return EncodeTxnReply(TxnRejected, message) }

func applyParticipantCommand(s participantStorage, region uint64, command TxnCommand) (sm.Result, error) {
	if command.Op != OpPrepare && command.Op != OpCommit && command.Op != OpAbort {
		return rejectTxn("unknown transaction operation"), nil
	}
	t, err := canonicalTransaction(command.Transaction, region)
	if err != nil {
		return rejectTxn(err.Error()), nil
	}
	record, exists, err := s.transaction(t.ID)
	if err != nil {
		return sm.Result{}, err
	}
	if exists && !sameTransaction(record.Transaction, t) {
		return rejectTxn("transaction ID is bound to different immutable metadata"), nil
	}
	if exists && record.State != TxnPrepared {
		if command.Op == OpPrepare || (command.Op == OpCommit && record.State == TxnCommitted) || (command.Op == OpAbort && record.State == TxnAborted) {
			return EncodeTxnReply(record.State, ""), nil
		}
		return rejectTxn("transaction final decision is irreversible"), nil
	}

	switch command.Op {
	case OpPrepare:
		if exists {
			return EncodeTxnReply(TxnPrepared, ""), nil
		}
		owner, err := s.lockOwner(t.Write.Key)
		if err != nil {
			return sm.Result{}, err
		}
		if owner != "" {
			return rejectTxn(fmt.Sprintf("key is locked by transaction %s", owner)), nil
		}
		if t.Write.Ver > 0 {
			version, err := s.version(t.Write.Key)
			if err != nil {
				return sm.Result{}, err
			}
			if version >= t.Write.Ver {
				return rejectTxn("transaction write version is stale"), nil
			}
		}
		if err := s.setTransaction(TxnRecord{Transaction: t, State: TxnPrepared}); err != nil {
			return sm.Result{}, err
		}
		if err := s.setLock(t.Write.Key, t.ID); err != nil {
			return sm.Result{}, err
		}
		return EncodeTxnReply(TxnPrepared, ""), nil
	case OpCommit:
		if !exists {
			return rejectTxn("commit requires a matching prepared transaction"), nil
		}
		owner, err := s.lockOwner(t.Write.Key)
		if err != nil {
			return sm.Result{}, err
		}
		if owner != t.ID {
			return sm.Result{}, errors.New("prepared transaction has lost its key lock")
		}
		if err := s.write(t.Write); err != nil {
			return sm.Result{}, err
		}
		if err := s.setTransaction(TxnRecord{Transaction: t, State: TxnCommitted}); err != nil {
			return sm.Result{}, err
		}
		if err := s.setLock(t.Write.Key, ""); err != nil {
			return sm.Result{}, err
		}
		return EncodeTxnReply(TxnCommitted, ""), nil
	case OpAbort:
		// Persist an abort tombstone even if PREPARE has not arrived yet.
		if exists {
			owner, err := s.lockOwner(t.Write.Key)
			if err != nil {
				return sm.Result{}, err
			}
			if owner != t.ID {
				return sm.Result{}, errors.New("prepared transaction has lost its key lock")
			}
			if err := s.setLock(t.Write.Key, ""); err != nil {
				return sm.Result{}, err
			}
		}
		if err := s.setTransaction(TxnRecord{Transaction: t, State: TxnAborted}); err != nil {
			return sm.Result{}, err
		}
		return EncodeTxnReply(TxnAborted, ""), nil
	}
	panic("unreachable transaction operation")
}

func applyLegacyWrite(s participantStorage, kv KVData) (bool, sm.Result, error) {
	if !validUserKey(kv.Key) {
		return false, rejectTxn("invalid user key"), nil
	}
	owner, err := s.lockOwner(kv.Key)
	if err != nil {
		return false, sm.Result{}, err
	}
	if owner != "" {
		return false, rejectTxn(fmt.Sprintf("key is locked by transaction %s", owner)), nil
	}
	if kv.Ver > 0 {
		version, err := s.version(kv.Key)
		if err != nil {
			return false, sm.Result{}, err
		}
		if version >= kv.Ver {
			return false, sm.Result{Value: 0}, nil
		}
	}
	if err := s.write(kv); err != nil {
		return false, sm.Result{}, err
	}
	return true, sm.Result{}, nil
}
