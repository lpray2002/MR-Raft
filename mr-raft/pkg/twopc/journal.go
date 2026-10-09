package twopc

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"

	"gitcode.com/lpray/multi-region-raft/pkg/mec"
)

const journalVersion = 1
const maxJournalFile = 16 << 20

var (
	ErrLocked  = errors.New("2PC journal already open")
	ErrCorrupt = errors.New("invalid 2PC journal")
	ErrJournal = errors.New("2PC journal persistence failed")
)

// Record retains the fixed transaction and final decision. Complete means all
// participants have acknowledged the final decision; incomplete final decisions
// must be redelivered, including aborts to participants whose prepare timed out.
type Record struct {
	Version     int             `json:"version"`
	Transaction mec.Transaction `json:"transaction"`
	State       mec.TxnState    `json:"state"`
	Complete    bool            `json:"complete"`
}

type journalOwner struct {
	Version     int    `json:"version"`
	Coordinator string `json:"coordinator"`
}

type envelope struct {
	Checksum string          `json:"sha256"`
	Payload  json.RawMessage `json:"payload"`
}

// Each transaction has one small atomic file. Updating a decision never rewrites
// the history of unrelated transactions. Final records are retained as retry
// tombstones; their garbage collection requires an external retention policy.
type journal struct {
	dir           string
	lock          *os.File
	fileSync      func(*os.File) error
	directorySync func(string) error
}

func syncDirectory(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func ensureDirectory(path string) error {
	info, err := os.Stat(path)
	if err == nil {
		if !info.IsDir() {
			return fmt.Errorf("journal path is not a directory: %s", path)
		}
		return nil
	}
	if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	parent := filepath.Dir(path)
	if parent == path {
		return err
	}
	if err := ensureDirectory(parent); err != nil {
		return err
	}
	if err := os.Mkdir(path, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return err
	}
	return syncDirectory(parent)
}

func openJournal(dir, coordinator string) (*journal, []Record, error) {
	dir, err := filepath.Abs(dir)
	if err != nil {
		return nil, nil, err
	}
	if err := ensureDirectory(dir); err != nil {
		return nil, nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, "LOCK"), os.O_CREATE|os.O_RDWR, 0600)
	if err != nil {
		return nil, nil, err
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		return nil, nil, fmt.Errorf("%w: %v", ErrLocked, err)
	}
	j := &journal{dir: dir, lock: f, fileSync: func(f *os.File) error { return f.Sync() }, directorySync: syncDirectory}
	fail := func(err error) (*journal, []Record, error) {
		j.close()
		return nil, nil, err
	}
	ownerPath := filepath.Join(dir, "coordinator.json")
	var owner journalOwner
	if err := readChecked(ownerPath, &owner); errors.Is(err, os.ErrNotExist) {
		files, scanErr := os.ReadDir(dir)
		if scanErr != nil {
			return fail(scanErr)
		}
		for _, file := range files {
			if file.Name() != "LOCK" && !strings.HasPrefix(file.Name(), ".tmp-") {
				return fail(fmt.Errorf("%w: records exist without a coordinator identity", ErrCorrupt))
			}
		}
		owner = journalOwner{Version: journalVersion, Coordinator: coordinator}
		if err := j.writeAtomic("coordinator.json", owner); err != nil {
			return fail(err)
		}
	} else if err != nil {
		return fail(fmt.Errorf("%w: coordinator identity: %v", ErrCorrupt, err))
	}
	if owner.Version != journalVersion || owner.Coordinator != coordinator {
		return fail(fmt.Errorf("%w: coordinator identity does not match", ErrCorrupt))
	}
	files, err := os.ReadDir(dir)
	if err != nil {
		return fail(err)
	}
	var records []Record
	for _, file := range files {
		name := file.Name()
		if name == "LOCK" || name == "coordinator.json" {
			continue
		}
		if strings.HasPrefix(name, ".tmp-") && !file.IsDir() {
			// A temporary file was never installed as a journal record. No
			// decision may have been sent on its authority.
			if err := os.Remove(filepath.Join(dir, name)); err != nil {
				return fail(err)
			}
			continue
		}
		if file.IsDir() || len(name) != 69 || !strings.HasSuffix(name, ".json") {
			return fail(fmt.Errorf("%w: unexpected journal entry %q", ErrCorrupt, name))
		}
		var record Record
		if err := readChecked(filepath.Join(dir, name), &record); err != nil {
			return fail(fmt.Errorf("%w: %s: %v", ErrCorrupt, name, err))
		}
		if err := validateRecord(record, coordinator); err != nil {
			return fail(fmt.Errorf("%w: %s: %v", ErrCorrupt, name, err))
		}
		if name != transactionFilename(record.Transaction.ID) {
			return fail(fmt.Errorf("%w: transaction filename mismatch", ErrCorrupt))
		}
		records = append(records, record)
	}
	if err := j.directorySync(dir); err != nil {
		return fail(fmt.Errorf("%w: %v", ErrJournal, err))
	}
	return j, records, nil
}

func transactionFilename(id string) string {
	sum := sha256.Sum256([]byte(id))
	return hex.EncodeToString(sum[:]) + ".json"
}

func readChecked(path string, dst interface{}) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxJournalFile+1))
	if err != nil {
		return err
	}
	if len(data) > maxJournalFile {
		return errors.New("journal record exceeds size limit")
	}
	var e envelope
	if err := strictJSON(data, &e); err != nil {
		return err
	}
	sum := sha256.Sum256(e.Payload)
	if e.Checksum != hex.EncodeToString(sum[:]) {
		return errors.New("journal checksum mismatch")
	}
	return strictJSON(e.Payload, dst)
}

func strictJSON(data []byte, dst interface{}) error {
	d := json.NewDecoder(bytes.NewReader(data))
	d.DisallowUnknownFields()
	if err := d.Decode(dst); err != nil {
		return err
	}
	var extra interface{}
	if err := d.Decode(&extra); err != io.EOF {
		return errors.New("trailing journal data")
	}
	return nil
}

func (j *journal) writeAtomic(name string, value interface{}) error {
	payload, err := json.Marshal(value)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(payload)
	data, err := json.Marshal(envelope{Checksum: hex.EncodeToString(sum[:]), Payload: payload})
	if err != nil {
		return err
	}
	if len(data) > maxJournalFile {
		return fmt.Errorf("%w: record exceeds size limit", ErrJournal)
	}
	f, err := os.CreateTemp(j.dir, ".tmp-")
	if err != nil {
		return fmt.Errorf("%w: %v", ErrJournal, err)
	}
	tmp := f.Name()
	defer os.Remove(tmp)
	if err := f.Chmod(0600); err != nil {
		f.Close()
		return fmt.Errorf("%w: %v", ErrJournal, err)
	}
	if _, err := f.Write(data); err != nil {
		f.Close()
		return fmt.Errorf("%w: %v", ErrJournal, err)
	}
	if err := j.fileSync(f); err != nil {
		f.Close()
		return fmt.Errorf("%w: file fsync: %v", ErrJournal, err)
	}
	if err := f.Close(); err != nil {
		return fmt.Errorf("%w: %v", ErrJournal, err)
	}
	if err := os.Rename(tmp, filepath.Join(j.dir, name)); err != nil {
		return fmt.Errorf("%w: %v", ErrJournal, err)
	}
	if err := j.directorySync(j.dir); err != nil {
		return fmt.Errorf("%w: directory fsync: %v", ErrJournal, err)
	}
	return nil
}

func (j *journal) save(record Record) error {
	return j.writeAtomic(transactionFilename(record.Transaction.ID), record)
}

func (j *journal) close() error {
	unlockErr := syscall.Flock(int(j.lock.Fd()), syscall.LOCK_UN)
	return errors.Join(unlockErr, j.lock.Close())
}
