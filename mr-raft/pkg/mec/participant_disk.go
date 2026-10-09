package mec

import (
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/cockroachdb/pebble"
)

type pebbleGetter interface {
	Get([]byte) ([]byte, io.Closer, error)
}

// Reads during Update use the indexed batch, including earlier entries' writes.
type diskParticipantStore struct {
	reader pebbleGetter
	batch  *pebble.Batch
}

func (s diskParticipantStore) transaction(id string) (TxnRecord, bool, error) {
	data, closer, err := s.reader.Get([]byte(txnRecordPrefix + id))
	if err == pebble.ErrNotFound {
		return TxnRecord{}, false, nil
	}
	if err != nil {
		return TxnRecord{}, false, err
	}
	defer closer.Close()
	var record TxnRecord
	if err := json.Unmarshal(data, &record); err != nil {
		return TxnRecord{}, false, err
	}
	return record, true, nil
}
func (s diskParticipantStore) setTransaction(record TxnRecord) error {
	data, err := json.Marshal(record)
	if err != nil {
		return err
	}
	return s.batch.Set([]byte(txnRecordPrefix+record.Transaction.ID), data, nil)
}
func (s diskParticipantStore) lockOwner(key string) (string, error) {
	data, closer, err := s.reader.Get([]byte(txnLockPrefix + key))
	if err == pebble.ErrNotFound {
		return "", nil
	}
	if err != nil {
		return "", err
	}
	defer closer.Close()
	return string(data), nil
}
func (s diskParticipantStore) setLock(key, id string) error {
	if id == "" {
		return s.batch.Delete([]byte(txnLockPrefix+key), nil)
	}
	return s.batch.Set([]byte(txnLockPrefix+key), []byte(id), nil)
}
func (s diskParticipantStore) version(key string) (uint64, error) {
	data, closer, err := s.reader.Get([]byte(verKeyPrefix + key))
	if err == pebble.ErrNotFound {
		return 0, nil
	}
	if err != nil {
		return 0, err
	}
	defer closer.Close()
	if len(data) != 8 {
		return 0, errors.New("invalid stored key version")
	}
	return binary.LittleEndian.Uint64(data), nil
}
func (s diskParticipantStore) write(kv KVData) error {
	if err := s.batch.Set([]byte(kv.Key), []byte(kv.Val), nil); err != nil {
		return err
	}
	if kv.Ver > 0 {
		version := make([]byte, 8)
		binary.LittleEndian.PutUint64(version, kv.Ver)
		return s.batch.Set([]byte(verKeyPrefix+kv.Key), version, nil)
	}
	return nil
}

func diskTransactionReply(db *pebble.DB, id string) (*TxnRecord, error) {
	record, exists, err := (diskParticipantStore{reader: db}).transaction(id)
	if err != nil {
		return nil, err
	}
	if !exists {
		return &TxnRecord{State: TxnUnknown, Transaction: Transaction{ID: id}}, nil
	}
	return &record, nil
}

func diskPendingTransactions(db *pebble.DB) (*PendingTxnResult, error) {
	iter := db.NewIter(&pebble.IterOptions{LowerBound: []byte(txnRecordPrefix), UpperBound: []byte("\x00txn\x01")})
	defer iter.Close()
	result := &PendingTxnResult{Transactions: make([]TxnRecord, 0)}
	for iter.First(); iter.Valid(); iter.Next() {
		var record TxnRecord
		if err := json.Unmarshal(iter.Value(), &record); err != nil {
			return nil, err
		}
		if record.State == TxnPrepared {
			result.Transactions = append(result.Transactions, record)
		}
	}
	return result, iter.Error()
}

func diskPendingLockError(db *pebble.DB) error {
	iter := db.NewIter(&pebble.IterOptions{LowerBound: []byte(txnLockPrefix), UpperBound: []byte("\x00txn-lock\x01")})
	defer iter.Close()
	if iter.First() {
		return &PendingTxnError{ID: string(iter.Value())}
	}
	return iter.Error()
}

func isTxnMetadata(key string) bool {
	return strings.HasPrefix(key, txnRecordPrefix) || strings.HasPrefix(key, txnLockPrefix)
}

// Snapshot/open must retain every prepared transaction's durable key lock.
func validateDiskTransactions(db *pebble.DB, region uint64) error {
	records := make(map[string]TxnRecord)
	locks := make(map[string]string)
	iter := db.NewIter(nil)
	defer iter.Close()
	for iter.First(); iter.Valid(); iter.Next() {
		key := string(iter.Key())
		if strings.HasPrefix(key, txnRecordPrefix) {
			var record TxnRecord
			if err := json.Unmarshal(iter.Value(), &record); err != nil {
				return err
			}
			records[strings.TrimPrefix(key, txnRecordPrefix)] = record
		} else if strings.HasPrefix(key, txnLockPrefix) {
			locks[strings.TrimPrefix(key, txnLockPrefix)] = string(iter.Value())
		}
	}
	if err := iter.Error(); err != nil {
		return err
	}
	expected, _, err := restoreMemoryTransactions(records, region)
	if err != nil {
		return err
	}
	if len(expected) != len(locks) {
		return errors.New("transaction lock index is inconsistent")
	}
	for key, owner := range expected {
		if locks[key] != owner {
			return fmt.Errorf("transaction lock for key %q is inconsistent", key)
		}
	}
	return nil
}
