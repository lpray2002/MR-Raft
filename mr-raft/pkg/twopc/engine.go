// Package twopc coordinates durable two-phase commit across independent Raft
// regions. Its journal belongs to the request coordinator, not the cluster
// manager. Prepared participants may remain blocked until that coordinator and
// its durable storage recover.
package twopc

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"sync"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
)

var (
	ErrClosed    = errors.New("2PC engine closed")
	ErrConflict  = errors.New("transaction ID reused with different contents")
	ErrAborted   = errors.New("transaction durably aborted")
	ErrUncertain = errors.New("transaction outcome not fully confirmed")
)

type Transport interface {
	Propose(context.Context, uint64, mec.TxnCommand) (mec.TxnReply, error)
}

type Outcome struct {
	State    mec.TxnState
	Complete bool
}

// UncertainError preserves a durable decision when one exists. In particular,
// State == committed never means the transaction can subsequently be aborted.
type UncertainError struct {
	State mec.TxnState
	Cause error
}

func (e *UncertainError) Error() string {
	return fmt.Sprintf("%s (state %s): %v", ErrUncertain, e.State, e.Cause)
}

func (e *UncertainError) Unwrap() []error {
	if e.Cause == nil {
		return []error{ErrUncertain}
	}
	return []error{ErrUncertain, e.Cause}
}

type entry struct {
	gate   chan struct{}
	record *Record // protected by Engine.mu, separately from the execution gate
}

type Engine struct {
	mu          sync.RWMutex
	entries     map[string]*entry
	pending     map[string]*entry // incomplete records only; protected by mu
	journal     *journal
	coordinator string
	transport   Transport
	// CleanupTimeout bounds independent abort cleanup and each recovery attempt.
	// Configure it before concurrently using the engine; nonpositive values use 2s.
	CleanupTimeout time.Duration
	active         sync.WaitGroup
	closed         bool
	fatal          error
	closeDone      chan struct{}
	closeErr       error
}

// Open locks a dedicated journal directory. Surviving undecided transactions
// are durably changed to abort before accepting work; Recover disseminates those
// aborts and all incomplete final decisions. Corruption and persistence errors
// fail closed rather than guessing a decision.
func Open(dir, coordinator string, transport Transport) (*Engine, error) {
	if coordinator == "" || transport == nil {
		return nil, errors.New("coordinator identity and transport are required")
	}
	j, records, err := openJournal(dir, coordinator)
	if err != nil {
		return nil, err
	}
	e := &Engine{entries: make(map[string]*entry), pending: make(map[string]*entry), journal: j, coordinator: coordinator,
		transport: transport, CleanupTimeout: 2 * time.Second, closeDone: make(chan struct{})}
	for _, record := range records {
		if record.State == mec.TxnUnknown {
			record.State = mec.TxnAborted
			if err := j.save(record); err != nil {
				j.close()
				return nil, err
			}
		}
		r := cloneRecord(record)
		item := &entry{gate: make(chan struct{}, 1), record: &r}
		e.entries[r.Transaction.ID] = item
		if !r.Complete {
			e.pending[r.Transaction.ID] = item
		}
	}
	return e, nil
}

func (e *Engine) begin() error {
	e.mu.Lock()
	defer e.mu.Unlock()
	if e.closed {
		return ErrClosed
	}
	if e.fatal != nil {
		return &UncertainError{State: mec.TxnUnknown, Cause: e.fatal}
	}
	e.active.Add(1)
	return nil
}

func (e *Engine) getEntry(id string) *entry {
	e.mu.Lock()
	defer e.mu.Unlock()
	item := e.entries[id]
	if item == nil {
		item = &entry{gate: make(chan struct{}, 1)}
		e.entries[id] = item
	}
	return item
}

func acquire(ctx context.Context, item *entry) error {
	select {
	case item.gate <- struct{}{}:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

func (e *Engine) snapshot(item *entry) (Record, bool) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	if item.record == nil {
		return Record{}, false
	}
	return cloneRecord(*item.record), true
}

func (e *Engine) persist(item *entry, record Record) error {
	if err := e.fault(); err != nil {
		return err
	}
	if err := e.journal.save(record); err != nil {
		e.mu.Lock()
		if e.fatal == nil {
			e.fatal = err
		}
		e.mu.Unlock()
		return err
	}
	e.mu.Lock()
	if e.fatal != nil {
		err := e.fatal
		e.mu.Unlock()
		return err
	}
	r := cloneRecord(record)
	item.record = &r
	if r.Complete {
		delete(e.pending, r.Transaction.ID)
	} else {
		e.pending[r.Transaction.ID] = item
	}
	e.mu.Unlock()
	return nil
}

func (e *Engine) fault() error {
	e.mu.RLock()
	defer e.mu.RUnlock()
	return e.fatal
}

func uncertain(record Record, err error) (Outcome, error) {
	return Outcome{State: record.State, Complete: record.Complete}, &UncertainError{State: record.State, Cause: err}
}

// Execute retries an existing immutable transaction. The stored participants
// are reused even if the caller's current topology provides a different list;
// changing the write for the same ID is rejected. The initial transaction must
// reach durable storage before any participant receives prepare.
func (e *Engine) Execute(ctx context.Context, id string, write mec.KVData, participants []uint64) (Outcome, error) {
	if err := e.begin(); err != nil {
		return Outcome{State: mec.TxnUnknown}, err
	}
	defer e.active.Done()
	if id == "" || write.Key == "" {
		return Outcome{State: mec.TxnUnknown}, errors.New("transaction ID and write key are required")
	}
	item := e.getEntry(id)
	if err := acquire(ctx, item); err != nil {
		return Outcome{State: mec.TxnUnknown}, &UncertainError{State: mec.TxnUnknown, Cause: err}
	}
	defer func() { <-item.gate }()
	if err := ctx.Err(); err != nil {
		return Outcome{State: mec.TxnUnknown}, &UncertainError{State: mec.TxnUnknown, Cause: err}
	}
	record, exists := e.snapshot(item)
	if err := e.fault(); err != nil {
		if !exists {
			record.State = mec.TxnUnknown
		}
		return uncertain(record, err)
	}
	if exists {
		if record.Transaction.Write != write {
			return Outcome{State: record.State, Complete: record.Complete}, ErrConflict
		}
	} else {
		ids, err := normalizeParticipants(participants)
		if err != nil {
			return Outcome{State: mec.TxnUnknown}, err
		}
		record = Record{Version: journalVersion, State: mec.TxnUnknown, Transaction: mec.Transaction{
			ID: id, Coordinator: e.coordinator, Participants: ids, Write: write}}
		if err := e.persist(item, record); err != nil {
			return uncertain(record, err)
		}
	}
	if record.State != mec.TxnUnknown {
		return e.finish(ctx, item, record, nil)
	}
	prepareErr := e.prepare(ctx, record.Transaction)
	if prepareErr == nil {
		prepareErr = ctx.Err()
	}
	if prepareErr != nil {
		record.State = mec.TxnAborted
		if err := e.persist(item, record); err != nil {
			record.State = mec.TxnUnknown
			return uncertain(record, err)
		}
		// A canceled client context cannot prevent abort tombstones reaching
		// participants, including a prepare that is still in transit.
		cleanupCtx, cancel := context.WithTimeout(context.Background(), e.cleanupDuration())
		defer cancel()
		return e.finish(cleanupCtx, item, record, prepareErr)
	}
	record.State = mec.TxnCommitted
	if err := e.persist(item, record); err != nil {
		record.State = mec.TxnUnknown
		return uncertain(record, err)
	}
	// Once the decision is durable, even a canceled context cannot change it.
	return e.finish(ctx, item, record, nil)
}

func (e *Engine) prepare(ctx context.Context, transaction mec.Transaction) error {
	if err := e.fault(); err != nil {
		return err
	}
	pctx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan error, len(transaction.Participants))
	for _, id := range transaction.Participants {
		go func(id uint64) {
			reply, err := e.transport.Propose(pctx, id, mec.TxnCommand{Op: mec.OpPrepare, Transaction: cloneTransaction(transaction)})
			if err == nil && (reply.Error != "" || (reply.State != mec.TxnPrepared && reply.State != mec.TxnCommitted)) {
				err = fmt.Errorf("region %d refused prepare (%s): %s", id, reply.State, reply.Error)
			}
			results <- err
		}(id)
	}
	for range transaction.Participants {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case err := <-results:
			if err != nil {
				return err
			}
		}
	}
	return nil
}

func (e *Engine) deliver(ctx context.Context, record Record) error {
	if record.State != mec.TxnCommitted && record.State != mec.TxnAborted {
		return errors.New("cannot disseminate an undecided transaction")
	}
	if err := e.fault(); err != nil {
		return err
	}
	op := mec.OpCommit
	if record.State == mec.TxnAborted {
		op = mec.OpAbort
	}
	results := make(chan error, len(record.Transaction.Participants))
	for _, id := range record.Transaction.Participants {
		go func(id uint64) {
			reply, err := e.transport.Propose(ctx, id, mec.TxnCommand{Op: op, Transaction: cloneTransaction(record.Transaction)})
			if err == nil && (reply.Error != "" || reply.State != record.State) {
				err = fmt.Errorf("region %d did not acknowledge %s (%s): %s", id, record.State, reply.State, reply.Error)
			}
			results <- err
		}(id)
	}
	var failures []error
	for range record.Transaction.Participants {
		select {
		case <-ctx.Done():
			return errors.Join(append(failures, ctx.Err())...)
		case err := <-results:
			if err != nil {
				failures = append(failures, err)
			}
		}
	}
	return errors.Join(failures...)
}

func (e *Engine) finish(ctx context.Context, item *entry, record Record, cause error) (Outcome, error) {
	if !record.Complete {
		if err := e.deliver(ctx, record); err != nil {
			return uncertain(record, errors.Join(cause, err))
		}
		record.Complete = true
		if err := e.persist(item, record); err != nil {
			record.Complete = false
			return uncertain(record, err)
		}
	}
	if record.State == mec.TxnAborted {
		return Outcome{State: record.State, Complete: record.Complete}, errors.Join(ErrAborted, cause)
	}
	return Outcome{State: record.State, Complete: record.Complete}, nil
}

// Recover redelivers incomplete final decisions. It never changes commit into
// abort and sends abort to the complete original participant set, not merely to
// the participants that acknowledged prepare. Call it periodically at the
// original coordinator while its local journal remains available.
func (e *Engine) Recover(ctx context.Context) error {
	if err := e.begin(); err != nil {
		return err
	}
	defer e.active.Done()
	var failures []error
	for _, record := range e.pendingRecords() {
		if err := ctx.Err(); err != nil {
			return errors.Join(append(failures, err)...)
		}
		if record.Complete {
			continue
		}
		item := e.getEntry(record.Transaction.ID)
		select {
		case item.gate <- struct{}{}:
		default:
			// An active Execute owns this transaction; do not confuse its
			// live prepare phase with an undecided record left by a crash.
			continue
		}
		current, _ := e.snapshot(item)
		if current.State == mec.TxnUnknown {
			current.State = mec.TxnAborted
			if err := e.persist(item, current); err != nil {
				<-item.gate
				return errors.Join(append(failures, err)...)
			}
		}
		rctx, cancel := context.WithTimeout(ctx, e.cleanupDuration())
		outcome, err := e.finish(rctx, item, current, nil)
		cancel()
		<-item.gate
		if err != nil && !(errors.Is(err, ErrAborted) && outcome.Complete) {
			failures = append(failures, fmt.Errorf("transaction %s: %w", current.Transaction.ID, err))
		}
	}
	return errors.Join(failures...)
}

func (e *Engine) Records() []Record {
	e.mu.RLock()
	defer e.mu.RUnlock()
	records := make([]Record, 0, len(e.entries))
	for _, item := range e.entries {
		if item.record != nil {
			records = append(records, cloneRecord(*item.record))
		}
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Transaction.ID < records[j].Transaction.ID })
	return records
}

// pendingRecords keeps periodic recovery proportional to unfinished work rather
// than cloning the full payload history retained for idempotent client retries.
func (e *Engine) pendingRecords() []Record {
	e.mu.RLock()
	defer e.mu.RUnlock()
	records := make([]Record, 0, len(e.pending))
	for _, item := range e.pending {
		records = append(records, cloneRecord(*item.record))
	}
	sort.Slice(records, func(i, j int) bool { return records[i].Transaction.ID < records[j].Transaction.ID })
	return records
}

func (e *Engine) Get(id string) (Record, bool) {
	e.mu.RLock()
	defer e.mu.RUnlock()
	item := e.entries[id]
	if item == nil || item.record == nil {
		return Record{}, false
	}
	return cloneRecord(*item.record), true
}

// Lookup is an alias for Get.
func (e *Engine) Lookup(id string) (Record, bool) { return e.Get(id) }

func (e *Engine) cleanupDuration() time.Duration {
	if e.CleanupTimeout <= 0 {
		return 2 * time.Second
	}
	return e.CleanupTimeout
}

func (e *Engine) PendingForRegion(id uint64) bool {
	e.mu.RLock()
	defer e.mu.RUnlock()
	for _, item := range e.pending {
		for _, participant := range item.record.Transaction.Participants {
			if participant == id {
				return true
			}
		}
	}
	return false
}

func (e *Engine) Close() error {
	e.mu.Lock()
	if e.closed {
		done := e.closeDone
		e.mu.Unlock()
		<-done
		return e.closeErr
	}
	e.closed = true
	e.mu.Unlock()
	e.active.Wait()
	err := e.journal.close()
	e.mu.Lock()
	e.closeErr = err
	close(e.closeDone)
	e.mu.Unlock()
	return err
}

func normalizeParticipants(ids []uint64) ([]uint64, error) {
	if len(ids) == 0 {
		return nil, errors.New("transaction has no participants")
	}
	out := append([]uint64(nil), ids...)
	sort.Slice(out, func(i, j int) bool { return out[i] < out[j] })
	for i, id := range out {
		if id == 0 || (i > 0 && id == out[i-1]) {
			return nil, errors.New("participants must be distinct nonzero region IDs")
		}
	}
	return out, nil
}

func validateRecord(record Record, coordinator string) error {
	if record.Version != journalVersion || record.Transaction.ID == "" || record.Transaction.Coordinator != coordinator || record.Transaction.Write.Key == "" {
		return errors.New("invalid immutable transaction")
	}
	ids, err := normalizeParticipants(record.Transaction.Participants)
	if err != nil {
		return err
	}
	for i, id := range ids {
		if record.Transaction.Participants[i] != id {
			return errors.New("noncanonical participant list")
		}
	}
	if record.State != mec.TxnUnknown && record.State != mec.TxnCommitted && record.State != mec.TxnAborted {
		return errors.New("invalid coordinator decision")
	}
	if record.State == mec.TxnUnknown && record.Complete {
		return errors.New("undecided transaction marked complete")
	}
	return nil
}

func cloneTransaction(transaction mec.Transaction) mec.Transaction {
	transaction.Participants = append([]uint64(nil), transaction.Participants...)
	return transaction
}

func cloneRecord(record Record) Record {
	record.Transaction = cloneTransaction(record.Transaction)
	return record
}
