package mec

import (
	"fmt"
	"sort"
)

type memoryParticipantStore struct{ m *MemoryKV }

func (s memoryParticipantStore) transaction(id string) (TxnRecord, bool, error) {
	r, ok := s.m.txns[id]
	return r, ok, nil
}
func (s memoryParticipantStore) setTransaction(r TxnRecord) error {
	s.m.txns[r.Transaction.ID] = r
	return nil
}
func (s memoryParticipantStore) lockOwner(key string) (string, error) { return s.m.locks[key], nil }
func (s memoryParticipantStore) setLock(key, id string) error {
	if id == "" {
		delete(s.m.locks, key)
	} else {
		s.m.locks[key] = id
	}
	return nil
}
func (s memoryParticipantStore) version(key string) (uint64, error) { return s.m.vers[key], nil }
func (s memoryParticipantStore) write(kv KVData) error {
	s.m.data[kv.Key] = kv.Val
	if kv.Ver > 0 {
		s.m.vers[kv.Key] = kv.Ver
	}
	return nil
}

func pendingLockError(locks map[string]string) error {
	keys := make([]string, 0, len(locks))
	for key := range locks {
		keys = append(keys, key)
	}
	if len(keys) == 0 {
		return nil
	}
	sort.Strings(keys)
	return &PendingTxnError{ID: locks[keys[0]]}
}

func (m *MemoryKV) transactionReply(id string) *TxnRecord {
	if record, ok := m.txns[id]; ok {
		copy := copyTxnRecord(record)
		return &copy
	}
	return &TxnRecord{State: TxnUnknown, Transaction: Transaction{ID: id}}
}

func (m *MemoryKV) pendingTransactions() *PendingTxnResult {
	result := &PendingTxnResult{Transactions: make([]TxnRecord, 0)}
	for _, record := range m.txns {
		if record.State == TxnPrepared {
			result.Transactions = append(result.Transactions, copyTxnRecord(record))
		}
	}
	sort.Slice(result.Transactions, func(i, j int) bool {
		return result.Transactions[i].Transaction.ID < result.Transactions[j].Transaction.ID
	})
	return result
}

func restoreMemoryTransactions(records map[string]TxnRecord, region uint64) (map[string]string, map[string]TxnRecord, error) {
	locks := make(map[string]string)
	transactions := make(map[string]TxnRecord, len(records))
	for id, record := range records {
		t, err := canonicalTransaction(record.Transaction, region)
		if err != nil || t.ID != id {
			return nil, nil, fmt.Errorf("invalid transaction %q in snapshot", id)
		}
		if record.State != TxnPrepared && record.State != TxnCommitted && record.State != TxnAborted {
			return nil, nil, fmt.Errorf("invalid state for snapshot transaction %q", id)
		}
		if record.State == TxnPrepared {
			if locks[t.Write.Key] != "" {
				return nil, nil, fmt.Errorf("conflicting prepared transactions for key %q", t.Write.Key)
			}
			locks[t.Write.Key] = id
		}
		record.Transaction = t
		transactions[id] = record
	}
	return locks, transactions, nil
}
