package twopc

import (
	"context"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
)

// This participant model deliberately keeps prepare invisible and retains abort
// tombstones. Hooks model delivery failures, lost acknowledgments and late
// prepares; real Raft integration is tested separately by the node package.
type participantTransport struct {
	mu        sync.Mutex
	states    map[uint64]map[string]mec.TxnState
	writes    map[uint64]map[string]string
	applies   map[uint64]int
	calls     map[string]int
	before    func(context.Context, uint64, mec.TxnCommand) error
	after     func(context.Context, uint64, mec.TxnCommand) error
	ignoreCtx func(uint64, mec.TxnCommand) bool
}

func newTransport() *participantTransport {
	return &participantTransport{states: make(map[uint64]map[string]mec.TxnState),
		writes: make(map[uint64]map[string]string), applies: make(map[uint64]int), calls: make(map[string]int)}
}

func callKey(id uint64, command mec.TxnCommand) string {
	return fmt.Sprintf("%s/%s/%d", command.Transaction.ID, command.Op, id)
}

func (p *participantTransport) Propose(ctx context.Context, id uint64, command mec.TxnCommand) (mec.TxnReply, error) {
	p.mu.Lock()
	p.calls[callKey(id, command)]++
	before, after, ignoreCtx := p.before, p.after, p.ignoreCtx
	p.mu.Unlock()
	if before != nil {
		if err := before(ctx, id, command); err != nil {
			return mec.TxnReply{}, err
		}
	}
	if ignoreCtx == nil || !ignoreCtx(id, command) {
		if err := ctx.Err(); err != nil {
			return mec.TxnReply{}, err
		}
	}
	p.mu.Lock()
	if p.states[id] == nil {
		p.states[id] = make(map[string]mec.TxnState)
		p.writes[id] = make(map[string]string)
	}
	tx := command.Transaction
	state := p.states[id][tx.ID]
	switch command.Op {
	case mec.OpPrepare:
		if state == "" {
			state = mec.TxnPrepared
		}
	case mec.OpCommit:
		if state == mec.TxnPrepared {
			state = mec.TxnCommitted
			p.writes[id][tx.Write.Key] = tx.Write.Val
			p.applies[id]++
		} else if state != mec.TxnCommitted {
			p.mu.Unlock()
			return mec.TxnReply{State: mec.TxnRejected, Error: "not prepared"}, nil
		}
	case mec.OpAbort:
		if state != mec.TxnCommitted {
			state = mec.TxnAborted
		}
	default:
		p.mu.Unlock()
		return mec.TxnReply{}, errors.New("unexpected phase")
	}
	p.states[id][tx.ID] = state
	p.mu.Unlock()
	if after != nil {
		if err := after(ctx, id, command); err != nil {
			return mec.TxnReply{}, err
		}
	}
	return mec.TxnReply{State: state}, nil
}

func (p *participantTransport) state(id uint64, tx string) mec.TxnState {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.states[id][tx]
}

func (p *participantTransport) count(id uint64, tx, op string) int {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.calls[callKey(id, mec.TxnCommand{Op: op, Transaction: mec.Transaction{ID: tx}})]
}

func (p *participantTransport) businessApplies() int {
	p.mu.Lock()
	defer p.mu.Unlock()
	var count int
	for _, n := range p.applies {
		count += n
	}
	return count
}

func openForTest(t *testing.T, dir string, transport Transport) *Engine {
	t.Helper()
	e, err := Open(dir, "node-1", transport)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { e.Close() })
	return e
}

func TestPrepareFailureAbortsEveryParticipantWithoutBusinessCommit(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if command.Op == mec.OpPrepare {
			var record Record
			if err := readChecked(filepath.Join(dir, transactionFilename(command.Transaction.ID)), &record); err != nil {
				t.Errorf("prepare preceded durable transaction record: %v", err)
			}
			if !reflect.DeepEqual(record.Transaction, command.Transaction) {
				t.Errorf("unexpected durable prepare record: %+v", record)
			}
			if id == 2 {
				return errors.New("region unavailable before prepare")
			}
		}
		return nil
	}
	e := openForTest(t, dir, p)
	outcome, err := e.Execute(context.Background(), "write-1", mec.KVData{Key: "key", Val: "new"}, []uint64{2, 1})
	if !errors.Is(err, ErrAborted) || !outcome.Complete || outcome.State != mec.TxnAborted {
		t.Fatalf("expected confirmed abort, got %+v, %v", outcome, err)
	}
	for _, id := range []uint64{1, 2} {
		if p.state(id, "write-1") != mec.TxnAborted || p.count(id, "write-1", mec.OpCommit) != 0 {
			t.Fatalf("region %d was not fenced by an abort tombstone", id)
		}
	}
	if p.businessApplies() != 0 {
		t.Fatal("prepare failure changed business state")
	}
}

func TestLostCommitAckAndRestartOnlyResendCommit(t *testing.T) {
	p := newTransport()
	p.after = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && command.Op == mec.OpCommit {
			return errors.New("commit applied but acknowledgment lost")
		}
		return nil
	}
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	write := mec.KVData{Key: "key", Val: "new", Ver: 1}
	outcome, err := e.Execute(context.Background(), "write-1", write, []uint64{1, 2})
	if !errors.Is(err, ErrUncertain) || outcome.State != mec.TxnCommitted || outcome.Complete {
		t.Fatalf("expected uncertain committed result, got %+v, %v", outcome, err)
	}
	if !e.PendingForRegion(1) || !e.PendingForRegion(2) {
		t.Fatal("incomplete decision must retain every participant")
	}
	if err := e.Close(); err != nil {
		t.Fatal(err)
	}
	p.mu.Lock()
	p.after = nil
	p.mu.Unlock()
	e = openForTest(t, dir, p)
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if p.businessApplies() != 2 {
		t.Fatalf("commit retries applied business writes more than once: %d", p.businessApplies())
	}
	for _, id := range []uint64{1, 2} {
		if p.count(id, "write-1", mec.OpAbort) != 0 || e.PendingForRegion(id) {
			t.Fatal("commit recovery must never abort or remain pending")
		}
	}
	outcome, err = e.Execute(context.Background(), "write-1", write, []uint64{3})
	if err != nil || !outcome.Complete || p.count(3, "write-1", mec.OpPrepare) != 0 {
		t.Fatalf("retry rebound fixed participants: %+v, %v", outcome, err)
	}
	write.Val = "different"
	if _, err := e.Execute(context.Background(), "write-1", write, []uint64{1, 2}); !errors.Is(err, ErrConflict) {
		t.Fatalf("changed payload did not conflict: %v", err)
	}
}

func TestCanceledClientAfterDurableCommitCannotAbort(t *testing.T) {
	p := newTransport()
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var once sync.Once
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if command.Op == mec.OpCommit {
			once.Do(cancel)
		}
		return nil
	}
	e := openForTest(t, t.TempDir(), p)
	outcome, err := e.Execute(ctx, "cancel-at-commit", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2})
	if !errors.Is(err, ErrUncertain) || outcome.State != mec.TxnCommitted || outcome.Complete {
		t.Fatalf("cancellation changed durable commit: %+v, %v", outcome, err)
	}
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, id := range []uint64{1, 2} {
		if p.state(id, "cancel-at-commit") != mec.TxnCommitted || p.count(id, "cancel-at-commit", mec.OpAbort) != 0 {
			t.Fatal("a committed decision was aborted after cancellation")
		}
	}
}

func TestRestartAbortsUndecidedTransactionIncludingUnknownParticipant(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	record := Record{Version: journalVersion, State: mec.TxnUnknown, Transaction: mec.Transaction{
		ID: "crashed-during-prepare", Coordinator: "node-1", Participants: []uint64{1, 2}, Write: mec.KVData{Key: "key", Val: "new"}}}
	if err := e.persist(e.getEntry(record.Transaction.ID), record); err != nil {
		t.Fatal(err)
	}
	if _, err := p.Propose(context.Background(), 1, mec.TxnCommand{Op: mec.OpPrepare, Transaction: record.Transaction}); err != nil {
		t.Fatal(err)
	}
	e.Close()
	e = openForTest(t, dir, p)
	loaded, ok := e.Get(record.Transaction.ID)
	if !ok || loaded.State != mec.TxnAborted || loaded.Complete {
		t.Fatalf("restart did not persist abort for undecided record: %+v", loaded)
	}
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	for _, id := range []uint64{1, 2} {
		if p.state(id, record.Transaction.ID) != mec.TxnAborted {
			t.Fatalf("unknown/prepared participant %d missing abort tombstone", id)
		}
	}
	if p.businessApplies() != 0 {
		t.Fatal("undecided recovery applied a business update")
	}
}

func TestInitialDirectorySyncFailureNeverSendsPrepare(t *testing.T) {
	p := newTransport()
	e := openForTest(t, t.TempDir(), p)
	e.journal.directorySync = func(string) error { return errors.New("injected directory fsync failure") }
	outcome, err := e.Execute(context.Background(), "initial-sync-failed", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2})
	if !errors.Is(err, ErrJournal) || !errors.Is(err, ErrUncertain) || outcome.State != mec.TxnUnknown {
		t.Fatalf("unexpected persistence outcome: %+v %v", outcome, err)
	}
	if p.count(1, "initial-sync-failed", mec.OpPrepare)+p.count(2, "initial-sync-failed", mec.OpPrepare) != 0 {
		t.Fatal("prepare sent before initial directory fsync succeeded")
	}
	if _, err := e.Execute(context.Background(), "later", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2}); !errors.Is(err, ErrJournal) {
		t.Fatalf("engine did not fail closed: %v", err)
	}
}

func TestDecisionFileSyncFailureNeverSendsCommitAndRestartAborts(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	var syncs int
	e.journal.fileSync = func(f *os.File) error {
		syncs++
		if syncs == 2 {
			return errors.New("injected decision file fsync failure")
		}
		return f.Sync()
	}
	outcome, err := e.Execute(context.Background(), "decision-sync-failed", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2})
	if !errors.Is(err, ErrJournal) || !errors.Is(err, ErrUncertain) || outcome.State != mec.TxnUnknown {
		t.Fatalf("unexpected persistence outcome: %+v %v", outcome, err)
	}
	for _, id := range []uint64{1, 2} {
		if p.count(id, "decision-sync-failed", mec.OpCommit) != 0 {
			t.Fatal("commit sent before decision file fsync succeeded")
		}
	}
	e.Close()
	e = openForTest(t, dir, p)
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if p.businessApplies() != 0 {
		t.Fatal("failed decision fsync resulted in business commit")
	}
}

func TestDecisionDirectorySyncFailureDoesNotGuessAbort(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	var syncs int
	e.journal.directorySync = func(dir string) error {
		syncs++
		if syncs == 2 {
			return errors.New("injected directory fsync failure after rename")
		}
		return syncDirectory(dir)
	}
	if _, err := e.Execute(context.Background(), "rename-uncertain", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2}); !errors.Is(err, ErrUncertain) {
		t.Fatal(err)
	}
	for _, id := range []uint64{1, 2} {
		if p.count(id, "rename-uncertain", mec.OpCommit) != 0 || p.count(id, "rename-uncertain", mec.OpAbort) != 0 {
			t.Fatal("uncertain durability caused a phase-two message")
		}
	}
	e.Close()
	// The renamed, file-synced record survives this process restart. A real
	// power loss may instead retain unknown, in which case Open chooses abort.
	e = openForTest(t, dir, p)
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if p.businessApplies() != 2 {
		t.Fatal("surviving commit decision was not recovered")
	}
}

func TestCanceledPrepareGetsIndependentAbortCleanupAndLatePrepareIsRejected(t *testing.T) {
	p := newTransport()
	lateEntered, releaseLate, lateDone := make(chan struct{}), make(chan struct{}), make(chan struct{})
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && command.Op == mec.OpPrepare {
			close(lateEntered)
			<-releaseLate // model a request already in the participant's network queue
		}
		if command.Op == mec.OpAbort && ctx.Err() != nil {
			t.Errorf("abort cleanup inherited canceled client context: %v", ctx.Err())
		}
		return nil
	}
	p.ignoreCtx = func(id uint64, command mec.TxnCommand) bool { return id == 2 && command.Op == mec.OpPrepare }
	p.after = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && command.Op == mec.OpPrepare {
			close(lateDone)
		}
		return nil
	}
	e := openForTest(t, t.TempDir(), p)
	ctx, cancel := context.WithCancel(context.Background())
	result := make(chan error, 1)
	go func() {
		outcome, err := e.Execute(ctx, "late-prepare", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2})
		if outcome.State != mec.TxnAborted || !outcome.Complete {
			result <- fmt.Errorf("abort cleanup incomplete: %+v %v", outcome, err)
			return
		}
		result <- err
	}()
	<-lateEntered
	cancel()
	if err := <-result; !errors.Is(err, ErrAborted) {
		t.Fatalf("expected confirmed abort after independent cleanup: %v", err)
	}
	close(releaseLate)
	<-lateDone
	if p.state(2, "late-prepare") != mec.TxnAborted || p.businessApplies() != 0 {
		t.Fatal("late prepare bypassed abort tombstone")
	}
}

func TestIncompleteAbortIsUncertainUntilAllAcknowledge(t *testing.T) {
	p := newTransport()
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && (command.Op == mec.OpPrepare || command.Op == mec.OpAbort) {
			return errors.New("participant offline")
		}
		return nil
	}
	e := openForTest(t, t.TempDir(), p)
	write := mec.KVData{Key: "key", Val: "new"}
	outcome, err := e.Execute(context.Background(), "abort-offline", write, []uint64{1, 2})
	if !errors.Is(err, ErrUncertain) || errors.Is(err, ErrAborted) || outcome.State != mec.TxnAborted || outcome.Complete {
		t.Fatalf("incomplete abort was reported as confirmed: %+v %v", outcome, err)
	}
	p.mu.Lock()
	p.before = nil
	p.mu.Unlock()
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	outcome, err = e.Execute(context.Background(), "abort-offline", write, []uint64{3})
	if !errors.Is(err, ErrAborted) || !outcome.Complete {
		t.Fatalf("recovered abort not confirmed: %+v %v", outcome, err)
	}
}

func TestAbortCleanupIsBoundedAndRemainsRecoverable(t *testing.T) {
	p := newTransport()
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && command.Op == mec.OpPrepare {
			return errors.New("prepare rejected")
		}
		if id == 2 && command.Op == mec.OpAbort {
			<-ctx.Done()
			return ctx.Err()
		}
		return nil
	}
	e := openForTest(t, t.TempDir(), p)
	e.CleanupTimeout = 20 * time.Millisecond
	start := time.Now()
	outcome, err := e.Execute(context.Background(), "abort-cleanup-timeout", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2})
	if !errors.Is(err, ErrUncertain) || !errors.Is(err, context.DeadlineExceeded) || outcome.State != mec.TxnAborted || outcome.Complete {
		t.Fatalf("cleanup timeout was not uncertain: %+v %v", outcome, err)
	}
	if time.Since(start) > time.Second || !e.PendingForRegion(2) {
		t.Fatal("cleanup was unbounded or forgot its pending participant")
	}
	p.mu.Lock()
	p.before = nil
	p.mu.Unlock()
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if record, _ := e.Get("abort-cleanup-timeout"); !record.Complete || record.State != mec.TxnAborted {
		t.Fatalf("timed-out abort was not recovered: %+v", record)
	}
}

func TestConcurrentRetriesSerializeOneIDAndRecoverySkipsActiveTransaction(t *testing.T) {
	p := newTransport()
	entered, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	p.before = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if command.Transaction.ID == "active" && id == 1 && command.Op == mec.OpPrepare {
			once.Do(func() { close(entered) })
			<-release
		}
		return nil
	}
	e := openForTest(t, t.TempDir(), p)
	write := mec.KVData{Key: "key", Val: "new"}
	const retries = 12
	results := make(chan error, retries)
	go func() { _, err := e.Execute(context.Background(), "active", write, []uint64{1, 2}); results <- err }()
	<-entered
	for i := 1; i < retries; i++ {
		go func() { _, err := e.Execute(context.Background(), "active", write, []uint64{3}); results <- err }()
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := e.Recover(ctx); err != nil {
		t.Fatalf("recovery waited for or aborted active prepare: %v", err)
	}
	if record, _ := e.Get("active"); record.State != mec.TxnUnknown {
		t.Fatalf("recovery changed live transaction decision: %+v", record)
	}
	if outcome, err := e.Execute(ctx, "independent", mec.KVData{Key: "other-key", Val: "value"}, []uint64{1, 2}); err != nil || !outcome.Complete {
		t.Fatalf("one active ID blocked another: %+v %v", outcome, err)
	}
	close(release)
	for i := 0; i < retries; i++ {
		if err := <-results; err != nil {
			t.Fatal(err)
		}
	}
	for _, id := range []uint64{1, 2} {
		if p.count(id, "active", mec.OpPrepare) != 1 || p.count(id, "active", mec.OpCommit) != 1 {
			t.Fatalf("concurrent retries duplicated transaction at region %d", id)
		}
	}
	if p.count(3, "active", mec.OpPrepare) != 0 {
		t.Fatal("retry changed immutable participant set")
	}
	copy, _ := e.Get("active")
	copy.Transaction.Participants[0] = 99
	all := e.Records()
	all[0].Transaction.Participants[0] = 88
	if original, _ := e.Get("active"); original.Transaction.Participants[0] != 1 {
		t.Fatal("Get or Records exposed mutable journal state")
	}
}

func TestLockIdentityAndCorruptionFailClosed(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	if second, err := Open(dir, "node-1", p); !errors.Is(err, ErrLocked) {
		if second != nil {
			second.Close()
		}
		t.Fatalf("journal did not exclude another coordinator process: %v", err)
	}
	if _, err := e.Execute(context.Background(), "corrupt", mec.KVData{Key: "key", Val: "new"}, []uint64{1, 2}); err != nil {
		t.Fatal(err)
	}
	e.Close()
	if _, err := Open(dir, "another-coordinator", p); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("journal allowed coordinator identity rebinding: %v", err)
	}
	path := filepath.Join(dir, transactionFilename("corrupt"))
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	data[len(data)/2] ^= 1
	if err := os.WriteFile(path, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(dir, "node-1", p); !errors.Is(err, ErrCorrupt) {
		t.Fatalf("corrupt journal was accepted: %v", err)
	}
}

func TestRecoveryIndexExcludesFinalizedHistoryAndRestoresIncompleteDecisions(t *testing.T) {
	p := newTransport()
	dir := t.TempDir()
	e := openForTest(t, dir, p)
	const history = 12
	for i := 0; i < history; i++ {
		id := fmt.Sprintf("complete-%02d", i)
		write := mec.KVData{Key: id, Val: strings.Repeat("payload", 4096)}
		if outcome, err := e.Execute(context.Background(), id, write, []uint64{3, 4}); err != nil || !outcome.Complete {
			t.Fatalf("failed to build finalized history: %+v %v", outcome, err)
		}
	}
	p.mu.Lock()
	p.after = func(ctx context.Context, id uint64, command mec.TxnCommand) error {
		if id == 2 && command.Op == mec.OpCommit {
			return errors.New("final acknowledgment lost")
		}
		return nil
	}
	p.mu.Unlock()
	if outcome, err := e.Execute(context.Background(), "unfinished", mec.KVData{Key: "key", Val: "value"}, []uint64{1, 2}); !errors.Is(err, ErrUncertain) || outcome.Complete {
		t.Fatalf("expected incomplete commit: %+v %v", outcome, err)
	}
	assertIndex := func() {
		t.Helper()
		pending := e.pendingRecords()
		if len(pending) != 1 || pending[0].Transaction.ID != "unfinished" || len(e.Records()) != history+1 {
			t.Fatalf("pending index included history or lost final records: pending=%+v history=%d", pending, len(e.Records()))
		}
		if !e.PendingForRegion(1) || !e.PendingForRegion(2) || e.PendingForRegion(3) || e.PendingForRegion(4) {
			t.Fatal("pending region index treated finalized transactions as unresolved")
		}
	}
	assertIndex()
	e.Close()
	e = openForTest(t, dir, p)
	assertIndex() // Open rebuilds the index without changing the journal schema.
	p.mu.Lock()
	p.after = nil
	p.mu.Unlock()
	if err := e.Recover(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(e.pendingRecords()) != 0 || e.PendingForRegion(1) || e.PendingForRegion(2) || len(e.Records()) != history+1 {
		t.Fatal("recovery failed to clear pending work or discarded retained history")
	}
	for i := 0; i < history; i++ {
		if p.count(3, fmt.Sprintf("complete-%02d", i), mec.OpCommit) != 1 {
			t.Fatal("recovery redelivered an already finalized transaction")
		}
	}
}
