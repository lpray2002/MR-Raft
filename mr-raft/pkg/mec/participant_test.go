package mec

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"reflect"
	"sync"
	"sync/atomic"
	"testing"
	"unsafe"

	sm "github.com/lni/dragonboat/v3/statemachine"
)

type participantHarness struct {
	update func([]byte) (sm.Result, error)
	lookup func(interface{}) (interface{}, error)
	disk   *DiskKV
	memory *MemoryKV
}

func newParticipantHarness(t *testing.T, disk bool) participantHarness {
	t.Helper()
	if !disk {
		m := NewMemoryKV(1, 1).(*MemoryKV)
		return participantHarness{update: m.Update, lookup: m.Lookup, memory: m}
	}
	db, err := createDB(t.TempDir())
	if err != nil {
		t.Fatal(err)
	}
	d := NewDiskKV(1, 1).(*DiskKV)
	atomic.StorePointer(&d.db, unsafe.Pointer(db))
	t.Cleanup(func() {
		if atomic.LoadPointer(&d.db) != nil {
			d.Close()
		}
	})
	return diskHarness(d)
}

func diskHarness(d *DiskKV) participantHarness {
	return participantHarness{disk: d, lookup: d.Lookup, update: func(cmd []byte) (sm.Result, error) {
		entries, err := d.Update([]sm.Entry{{Index: d.lastApplied + 1, Cmd: cmd}})
		if err != nil {
			return sm.Result{}, err
		}
		return entries[0].Result, nil
	}}
}

func marshalCommand(t *testing.T, v interface{}) []byte {
	t.Helper()
	data, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func testTransaction(id, key string, version uint64) Transaction {
	return Transaction{ID: id, Coordinator: "mec-1", Participants: []uint64{2, 1}, Write: KVData{Key: key, Val: "new-" + id, Ver: version}}
}

func expectTxn(t *testing.T, h participantHarness, op string, txn Transaction, state TxnState) {
	t.Helper()
	result, err := h.update(marshalCommand(t, TxnCommand{Op: op, Transaction: txn}))
	if err != nil {
		t.Fatalf("%s returned a fatal Update error: %v", op, err)
	}
	reply, err := DecodeTxnReply(result)
	if err != nil || reply.State != state {
		t.Fatalf("%s reply=%+v err=%v, want %s", op, reply, err, state)
	}
	if state == TxnRejected && reply.Error == "" {
		t.Fatal("rejected command has no reason")
	}
}

func legacyWrite(t *testing.T, h participantHarness, kv KVData) sm.Result {
	t.Helper()
	result, err := h.update(marshalCommand(t, kv))
	if err != nil {
		t.Fatal(err)
	}
	return result
}

func expectValue(t *testing.T, h participantHarness, key, value string, version uint64) {
	t.Helper()
	result, err := h.lookup(&KeyQuery{Key: key})
	if err != nil {
		t.Fatal(err)
	}
	kv := result.(*KVResult)
	if !kv.Found || kv.Val != value || kv.Ver != version {
		t.Fatalf("key %s: %+v, want %q/%d", key, kv, value, version)
	}
}

func TestParticipantPrepareVisibilityAndCommit(t *testing.T) {
	for _, disk := range []bool{false, true} {
		t.Run(map[bool]string{false: "memory", true: "disk"}[disk], func(t *testing.T) {
			h := newParticipantHarness(t, disk)
			legacyWrite(t, h, KVData{Key: "task", Val: "old", Ver: 1})
			legacyWrite(t, h, KVData{Key: "other", Val: "unaffected", Ver: 1})
			txn := testTransaction("one", "task", 2)
			expectTxn(t, h, OpPrepare, txn, TxnPrepared)
			expectTxn(t, h, OpPrepare, txn, TxnPrepared)
			for _, query := range []interface{}{&KeyQuery{Key: "task"}, "task", []byte("task"), &ScanQuery{}, &ScanQuery{Limit: 1}} {
				if value, err := h.lookup(query); !errors.Is(err, ErrTxnPending) || value != nil {
					t.Fatalf("query %#v leaked prepared state: %#v, %v", query, value, err)
				}
			}
			expectValue(t, h, "other", "unaffected", 1)
			reply, err := DecodeTxnReply(legacyWrite(t, h, KVData{Key: "task", Val: "bypass", Ver: 9}))
			if err != nil || reply.State != TxnRejected {
				t.Fatalf("legacy write bypassed lock: %+v %v", reply, err)
			}
			pending, err := h.lookup(&PendingTxnQuery{})
			if err != nil || len(pending.(*PendingTxnResult).Transactions) != 1 {
				t.Fatalf("pending: %#v %v", pending, err)
			}
			expectTxn(t, h, OpCommit, txn, TxnCommitted)
			expectTxn(t, h, OpCommit, txn, TxnCommitted)
			expectTxn(t, h, OpPrepare, txn, TxnCommitted)
			expectTxn(t, h, OpAbort, txn, TxnRejected)
			expectValue(t, h, "task", txn.Write.Val, 2)
			pending, err = h.lookup(PendingTxnQuery{})
			if err != nil || len(pending.(*PendingTxnResult).Transactions) != 0 {
				t.Fatalf("final transaction remains pending: %#v %v", pending, err)
			}
			scan, err := h.lookup(&ScanQuery{})
			if err != nil || len(scan.(*ScanResult).KVs) != 2 {
				t.Fatalf("scan exposes bookkeeping or loses data: %#v %v", scan, err)
			}
		})
	}
}

func TestParticipantAbortTombstoneAndImmutableIdentity(t *testing.T) {
	for _, disk := range []bool{false, true} {
		t.Run(map[bool]string{false: "memory", true: "disk"}[disk], func(t *testing.T) {
			h := newParticipantHarness(t, disk)
			unknown := testTransaction("unknown", "task", 1)
			expectTxn(t, h, OpCommit, unknown, TxnRejected)
			record, _ := h.lookup(&TxnQuery{ID: unknown.ID})
			if record.(*TxnRecord).State != TxnUnknown {
				t.Fatal("commit without prepare mutated state")
			}
			expectTxn(t, h, OpAbort, unknown, TxnAborted)
			expectTxn(t, h, OpAbort, unknown, TxnAborted)
			expectTxn(t, h, OpPrepare, unknown, TxnAborted)
			expectTxn(t, h, OpCommit, unknown, TxnRejected)
			active := testTransaction("active", "task", 1)
			expectTxn(t, h, OpPrepare, active, TxnPrepared)
			other := testTransaction("other", "task", 2)
			expectTxn(t, h, OpPrepare, other, TxnRejected)
			expectTxn(t, h, OpAbort, other, TxnAborted)
			if _, err := h.lookup("task"); !errors.Is(err, ErrTxnPending) {
				t.Fatal("another transaction's abort released the active lock")
			}
			for _, mutate := range []func(*Transaction){
				func(x *Transaction) { x.Coordinator = "different" },
				func(x *Transaction) { x.Write.Val = "different" },
				func(x *Transaction) { x.Write.Key = "different" },
				func(x *Transaction) { x.Write.Ver++ },
				func(x *Transaction) { x.Participants = []uint64{1, 3} },
			} {
				changed := active
				mutate(&changed)
				for _, op := range []string{OpPrepare, OpCommit, OpAbort} {
					expectTxn(t, h, op, changed, TxnRejected)
				}
			}
			// A participant set is immutable as a set, not dependent on slice order.
			reordered := active
			reordered.Participants = []uint64{1, 2}
			expectTxn(t, h, OpPrepare, reordered, TxnPrepared)
			expectTxn(t, h, OpAbort, active, TxnAborted)
			expectTxn(t, h, OpPrepare, active, TxnAborted)
			expectTxn(t, h, OpCommit, active, TxnRejected)
			legacyWrite(t, h, KVData{Key: "task", Val: "after-abort", Ver: 3})
			expectValue(t, h, "task", "after-abort", 3)
		})
	}
}

func TestParticipantVersionAndRejectedCommands(t *testing.T) {
	for _, disk := range []bool{false, true} {
		t.Run(map[bool]string{false: "memory", true: "disk"}[disk], func(t *testing.T) {
			h := newParticipantHarness(t, disk)
			legacyWrite(t, h, KVData{Key: "task", Val: "latest", Ver: 5})
			legacyWrite(t, h, KVData{Key: "task", Val: "old-copy", Ver: 2})
			for _, v := range []uint64{2, 5} {
				expectTxn(t, h, OpPrepare, testTransaction("stale", "task", v), TxnRejected)
			}
			expectValue(t, h, "task", "latest", 5)
			valid := testTransaction("valid", "task", 6)
			invalid := []Transaction{valid, valid, valid, valid, valid}
			invalid[0].Coordinator = ""
			invalid[1].Participants = []uint64{2}
			invalid[2].Participants = []uint64{1, 1}
			invalid[3].Write.Key = txnRecordPrefix + "inject"
			invalid[4].ID = ""
			for _, txn := range invalid {
				expectTxn(t, h, OpPrepare, txn, TxnRejected)
			}
			for _, cmd := range [][]byte{[]byte("{"), []byte("{}"), marshalCommand(t, TxnCommand{Op: "invalid", Transaction: valid}), marshalCommand(t, KVData{Key: appliedIndexKey, Val: "corrupt"})} {
				result, err := h.update(cmd)
				if err != nil {
					t.Fatalf("business rejection became fatal Update error: %v", err)
				}
				reply, err := DecodeTxnReply(result)
				if err != nil || reply.State != TxnRejected {
					t.Fatalf("bad command reply: %+v %v", reply, err)
				}
			}
			expectTxn(t, h, OpPrepare, valid, TxnPrepared)
			expectTxn(t, h, OpCommit, valid, TxnCommitted)
			// The stored version now equals this transaction's write: retry must
			// resolve the existing ID before applying first-prepare validation.
			expectTxn(t, h, OpPrepare, valid, TxnCommitted)
		})
	}
}

func TestMemoryParticipantSnapshotRestoresLocksAndTombstones(t *testing.T) {
	h := newParticipantHarness(t, false)
	prepared := testTransaction("pending", "task", 2)
	committed := testTransaction("committed", "done", 1)
	aborted := testTransaction("aborted", "cancelled", 1)
	legacyWrite(t, h, KVData{Key: "task", Val: "old", Ver: 1})
	expectTxn(t, h, OpPrepare, prepared, TxnPrepared)
	expectTxn(t, h, OpPrepare, committed, TxnPrepared)
	expectTxn(t, h, OpCommit, committed, TxnCommitted)
	expectTxn(t, h, OpAbort, aborted, TxnAborted)
	var snapshot bytes.Buffer
	if err := h.memory.SaveSnapshot(&snapshot, nil, nil); err != nil {
		t.Fatal(err)
	}
	restored := newParticipantHarness(t, false)
	if err := restored.memory.RecoverFromSnapshot(bytes.NewReader(snapshot.Bytes()), nil, nil); err != nil {
		t.Fatal(err)
	}
	if _, err := restored.lookup(&ScanQuery{}); !errors.Is(err, ErrTxnPending) {
		t.Fatal("snapshot lost prepared lock")
	}
	expectValue(t, restored, "done", committed.Write.Val, 1)
	expectTxn(t, restored, OpPrepare, aborted, TxnAborted)
	if err := restored.memory.RecoverFromSnapshot(bytes.NewReader([]byte("{")), nil, nil); err == nil {
		t.Fatal("corrupt snapshot accepted")
	}
	if _, err := restored.lookup("task"); !errors.Is(err, ErrTxnPending) {
		t.Fatal("failed recovery mutated live state")
	}
	expectTxn(t, restored, OpCommit, prepared, TxnCommitted)
	expectValue(t, restored, "task", prepared.Write.Val, 2)
	// Queries must not expose mutable aliases of the durable identity.
	record, _ := restored.lookup(&TxnQuery{ID: prepared.ID})
	record.(*TxnRecord).Transaction.Participants[0] = 99
	expectTxn(t, restored, OpCommit, prepared, TxnCommitted)
}

func TestDiskParticipantIndexedBatch(t *testing.T) {
	h := newParticipantHarness(t, true)
	applyBatch := func(commands ...interface{}) []sm.Entry {
		entries := make([]sm.Entry, len(commands))
		for i, command := range commands {
			entries[i] = sm.Entry{Index: h.disk.lastApplied + uint64(i) + 1, Cmd: marshalCommand(t, command)}
		}
		out, err := h.disk.Update(entries)
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	applyBatch(KVData{Key: "task", Val: "latest", Ver: 5}, KVData{Key: "task", Val: "old", Ver: 2})
	expectValue(t, h, "task", "latest", 5)
	txn := testTransaction("six", "task", 6)
	entries := applyBatch(TxnCommand{Op: OpPrepare, Transaction: txn}, KVData{Key: "task", Val: "bypass", Ver: 8})
	if reply, _ := DecodeTxnReply(entries[1].Result); reply.State != TxnRejected {
		t.Fatal("same-batch legacy write bypassed prepare lock")
	}
	next := testTransaction("seven", "task", 7)
	applyBatch(TxnCommand{Op: OpCommit, Transaction: txn}, TxnCommand{Op: OpPrepare, Transaction: next}, TxnCommand{Op: OpAbort, Transaction: next})
	expectValue(t, h, "task", txn.Write.Val, 6)
	stale := testTransaction("stale-after-ten", "task", 9)
	entries = applyBatch(KVData{Key: "task", Val: "ten", Ver: 10}, TxnCommand{Op: OpPrepare, Transaction: stale})
	if reply, _ := DecodeTxnReply(entries[1].Result); reply.State != TxnRejected {
		t.Fatal("prepare read an old database version instead of indexed batch")
	}
	future := testTransaction("future", "new-key", 1)
	entries = applyBatch(TxnCommand{Op: OpAbort, Transaction: future}, TxnCommand{Op: OpPrepare, Transaction: future})
	if reply, _ := DecodeTxnReply(entries[1].Result); reply.State != TxnAborted {
		t.Fatal("same-batch late prepare bypassed abort tombstone")
	}
	if scan, err := h.lookup(&ScanQuery{}); err != nil || !reflect.DeepEqual(scan.(*ScanResult).KVs, map[string]string{"task": "ten"}) {
		t.Fatalf("unexpected scan %#v %v", scan, err)
	}
}

func inTemporaryWorkingDirectory(t *testing.T) {
	t.Helper()
	previous, err := os.Getwd()
	if err != nil {
		t.Fatal(err)
	}
	if err := os.Chdir(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := os.Chdir(previous); err != nil {
			t.Error(err)
		}
	})
}

func openTestDisk(t *testing.T, node uint64) (*DiskKV, uint64) {
	t.Helper()
	d := NewDiskKV(1, node).(*DiskKV)
	index, err := d.Open(nil)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if atomic.LoadPointer(&d.db) != nil {
			d.Close()
		}
	})
	return d, index
}

func TestDiskParticipantReopenAndSnapshot(t *testing.T) {
	inTemporaryWorkingDirectory(t)
	d, index := openTestDisk(t, 1)
	if index != 0 {
		t.Fatal(index)
	}
	h := diskHarness(d)
	txn := testTransaction("durable", "task", 2)
	legacyWrite(t, h, KVData{Key: "task", Val: "old", Ver: 1})
	expectTxn(t, h, OpPrepare, txn, TxnPrepared)
	ctx, err := d.PrepareSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	var snapshot bytes.Buffer
	if err := d.SaveSnapshot(ctx, &snapshot, nil); err != nil {
		t.Fatal(err)
	}
	if err := d.Close(); err != nil {
		t.Fatal(err)
	}
	reopened, index := openTestDisk(t, 1)
	if index != 2 {
		t.Fatalf("reopen lost applied index: %d", index)
	}
	reopenHarness := diskHarness(reopened)
	if _, err := reopenHarness.lookup("task"); !errors.Is(err, ErrTxnPending) {
		t.Fatal("reopen lost lock")
	}
	expectTxn(t, reopenHarness, OpCommit, txn, TxnCommitted)
	expectValue(t, reopenHarness, "task", txn.Write.Val, 2)
	recovered, _ := openTestDisk(t, 2)
	if err := recovered.RecoverFromSnapshot(bytes.NewReader(snapshot.Bytes()[:12]), nil); err == nil {
		t.Fatal("truncated snapshot accepted")
	}
	if recovered.lastApplied != 0 {
		t.Fatal("failed snapshot changed applied index")
	}
	if err := recovered.RecoverFromSnapshot(bytes.NewReader(snapshot.Bytes()), nil); err != nil {
		t.Fatal(err)
	}
	recoveryHarness := diskHarness(recovered)
	if recovered.lastApplied != 2 {
		t.Fatal("snapshot lost applied index")
	}
	if _, err := recoveryHarness.lookup(&KeyQuery{Key: "task"}); !errors.Is(err, ErrTxnPending) {
		t.Fatal("snapshot lost durable pending lock")
	}
	expectTxn(t, recoveryHarness, OpAbort, txn, TxnAborted)
	expectValue(t, recoveryHarness, "task", "old", 1)
	if err := recovered.Close(); err != nil {
		t.Fatal(err)
	}
	again, _ := openTestDisk(t, 2)
	expectTxn(t, diskHarness(again), OpPrepare, txn, TxnAborted)
}

type failingSnapshotWriter struct{}

func (failingSnapshotWriter) Write([]byte) (int, error) { return 0, io.ErrClosedPipe }

func TestParticipantSnapshotWriteFailure(t *testing.T) {
	h := newParticipantHarness(t, false)
	txn := testTransaction("one", "task", 1)
	expectTxn(t, h, OpPrepare, txn, TxnPrepared)
	if err := h.memory.SaveSnapshot(failingSnapshotWriter{}, nil, nil); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("snapshot write failure lost: %v", err)
	}
	disk := newParticipantHarness(t, true)
	expectTxn(t, disk, OpPrepare, txn, TxnPrepared)
	ctx, err := disk.disk.PrepareSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	if err := disk.disk.SaveSnapshot(ctx, failingSnapshotWriter{}, nil); !errors.Is(err, io.ErrClosedPipe) {
		t.Fatalf("disk snapshot write failure lost: %v", err)
	}
}

func TestDiskStorageFailureRollsBackApplyBatch(t *testing.T) {
	h := newParticipantHarness(t, true)
	db := (*pebbledb)(atomic.LoadPointer(&h.disk.db))
	// A malformed stored version models an unreadable durable metadata record.
	if err := db.db.Set([]byte(verKeyPrefix+"bad"), []byte{1}, db.syncwo); err != nil {
		t.Fatal(err)
	}
	txn := testTransaction("fail", "bad", 2)
	_, err := h.disk.Update([]sm.Entry{
		{Index: 1, Cmd: marshalCommand(t, KVData{Key: "first", Val: "must-not-land", Ver: 1})},
		{Index: 2, Cmd: marshalCommand(t, TxnCommand{Op: OpPrepare, Transaction: txn})},
	})
	if err == nil {
		t.Fatal("durable metadata corruption did not surface a storage error")
	}
	if h.disk.lastApplied != 0 {
		t.Fatal("failed batch advanced the applied index")
	}
	result, err := h.lookup(&KeyQuery{Key: "first"})
	if err != nil || result.(*KVResult).Found {
		t.Fatalf("failed batch partially applied a preceding write: %#v %v", result, err)
	}
	record, err := h.lookup(&TxnQuery{ID: txn.ID})
	if err != nil || record.(*TxnRecord).State != TxnUnknown {
		t.Fatalf("failed batch installed pending state: %#v %v", record, err)
	}
	if err := db.db.Delete([]byte(verKeyPrefix+"bad"), db.syncwo); err != nil {
		t.Fatal(err)
	}
	expectTxn(t, h, OpPrepare, txn, TxnPrepared)
	expectTxn(t, h, OpAbort, txn, TxnAborted)
}

func TestParticipantConcurrentReadsDuringResolution(t *testing.T) {
	for _, disk := range []bool{false, true} {
		t.Run(map[bool]string{false: "memory", true: "disk"}[disk], func(t *testing.T) {
			h := newParticipantHarness(t, disk)
			legacyWrite(t, h, KVData{Key: "task", Val: "initial"})
			done := make(chan struct{})
			failures := make(chan error, 4)
			var readers sync.WaitGroup
			var stopOnce sync.Once
			stopReaders := func() { stopOnce.Do(func() { close(done); readers.Wait() }) }
			defer stopReaders()
			for i := 0; i < 4; i++ {
				readers.Add(1)
				go func() {
					defer readers.Done()
					for {
						select {
						case <-done:
							return
						default:
						}
						for _, query := range []interface{}{&KeyQuery{Key: "task"}, "task", []byte("task"), &ScanQuery{}, &PendingTxnQuery{}} {
							if _, err := h.lookup(query); err != nil && !errors.Is(err, ErrTxnPending) {
								failures <- err
								return
							}
						}
					}
				}()
			}
			for i := 0; i < 30; i++ {
				txn := testTransaction(fmt.Sprintf("concurrent-%d", i), "task", 0)
				expectTxn(t, h, OpPrepare, txn, TxnPrepared)
				if i%2 == 0 {
					expectTxn(t, h, OpCommit, txn, TxnCommitted)
				} else {
					expectTxn(t, h, OpAbort, txn, TxnAborted)
				}
			}
			stopReaders()
			close(failures)
			for err := range failures {
				t.Error(err)
			}
		})
	}
}

func TestDiskSnapshotPreservesBinaryVersionAndAppliedIndex(t *testing.T) {
	inTemporaryWorkingDirectory(t)
	d, _ := openTestDisk(t, 1)
	version := uint64(1<<63) + 255
	entries, err := d.Update([]sm.Entry{{Index: 200, Cmd: marshalCommand(t, KVData{Key: "task", Val: "old", Ver: version})}})
	if err != nil || len(entries) != 1 {
		t.Fatalf("seed: %v", err)
	}
	h := diskHarness(d)
	txn := testTransaction("binary", "task", version+1)
	expectTxn(t, h, OpPrepare, txn, TxnPrepared)
	ctx, err := d.PrepareSnapshot()
	if err != nil {
		t.Fatal(err)
	}
	var snapshot bytes.Buffer
	if err := d.SaveSnapshot(ctx, &snapshot, nil); err != nil {
		t.Fatal(err)
	}
	restored, _ := openTestDisk(t, 2)
	if err := restored.RecoverFromSnapshot(bytes.NewReader(snapshot.Bytes()), nil); err != nil {
		t.Fatal(err)
	}
	if restored.lastApplied != 201 {
		t.Fatalf("binary applied index damaged: %d", restored.lastApplied)
	}
	restoredHarness := diskHarness(restored)
	expectTxn(t, restoredHarness, OpAbort, txn, TxnAborted)
	expectValue(t, restoredHarness, "task", "old", version)
}

func TestDiskSnapshotReadsLegacyStringValues(t *testing.T) {
	inTemporaryWorkingDirectory(t)
	d, _ := openTestDisk(t, 1)
	index := make([]byte, 8)
	binary.LittleEndian.PutUint64(index, 2)
	legacyRecords := []KVData{{Key: appliedIndexKey, Val: string(index)}, {Key: "legacy", Val: "old-format"}}
	var snapshot bytes.Buffer
	size := make([]byte, 8)
	binary.LittleEndian.PutUint64(size, uint64(len(legacyRecords)))
	snapshot.Write(size)
	for _, record := range legacyRecords {
		encoded := marshalCommand(t, record)
		binary.LittleEndian.PutUint64(size, uint64(len(encoded)))
		snapshot.Write(size)
		snapshot.Write(encoded)
	}
	if err := d.RecoverFromSnapshot(bytes.NewReader(snapshot.Bytes()), nil); err != nil {
		t.Fatal(err)
	}
	if d.lastApplied != 2 {
		t.Fatalf("legacy applied index lost: %d", d.lastApplied)
	}
	expectValue(t, diskHarness(d), "legacy", "old-format", 0)
}
