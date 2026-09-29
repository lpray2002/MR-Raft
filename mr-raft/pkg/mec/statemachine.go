// // Copyright 2017-2025 Based on Dragonboat examples
// //
// // DiskKV State Machine for Multi-Region Raft
package mec

//
//import (
//	"bytes"
//	"crypto/md5"
//	"encoding/binary"
//	"encoding/json"
//	"errors"
//	"fmt"
//	"io"
//	"io/ioutil"
//	"math/rand"
//	"os"
//	"path/filepath"
//	"runtime"
//	"sync"
//	"sync/atomic"
//	"time"
//	"unsafe"
//
//	"github.com/cockroachdb/pebble"
//	sm "github.com/lni/dragonboat/v3/statemachine"
//)
//
//const (
//	appliedIndexKey    string = "disk_kv_applied_index"
//	currentDBFilename  string = "current"
//	updatingDBFilename string = "current.updating"
//)
//
//func syncDir(dir string) (err error) {
//
//	if runtime.GOOS == "windows" {
//		return nil
//	}
//	fileInfo, err := os.Stat(dir)
//	if err != nil {
//		return err
//	}
//	if !fileInfo.IsDir() {
//		panic("not a dir")
//	}
//	df, err := os.Open(filepath.Clean(dir))
//	if err != nil {
//		return err
//	}
//	defer func() {
//		if cerr := df.Close(); err == nil {
//			err = cerr
//		}
//	}()
//	return df.Sync()
//}
//
//type KVData struct {
//	Key string
//	Val string
//}
//
//// pebbledb is a wrapper to ensure lookup() and close() can be concurrently invoked
//type pebbledb struct {
//	mu     sync.RWMutex
//	db     *pebble.DB
//	ro     *pebble.IterOptions
//	wo     *pebble.WriteOptions
//	syncwo *pebble.WriteOptions
//	closed bool
//}
//
//func (r *pebbledb) lookup(query []byte) ([]byte, error) {
//	r.mu.RLock()
//	defer r.mu.RUnlock()
//	if r.closed {
//		return nil, errors.New("db already closed")
//	}
//	val, closer, err := r.db.Get(query)
//	if err != nil {
//		return nil, err
//	}
//	defer closer.Close()
//	if len(val) == 0 {
//		return []byte(""), nil
//	}
//	buf := make([]byte, len(val))
//	copy(buf, val)
//	return buf, nil
//}
//
//func (r *pebbledb) close() {
//	r.mu.Lock()
//	defer r.mu.Unlock()
//	r.closed = true
//	if r.db != nil {
//		r.db.Close()
//	}
//}
//
//// createDB creates a PebbleDB with specified optimization level
//func createDB(dbdir string) (*pebbledb, error) {
//	ro := &pebble.IterOptions{}
//	wo := &pebble.WriteOptions{Sync: false}
//	syncwo := &pebble.WriteOptions{Sync: true}
//	cache := pebble.NewCache(0)
//	opts := &pebble.Options{
//		MaxManifestFileSize: 1024 * 32,
//		MemTableSize:        1024 * 32,
//		Cache:               cache,
//	}
//	if err := os.MkdirAll(dbdir, 0755); err != nil {
//		return nil, err
//	}
//	db, err := pebble.Open(dbdir, opts)
//	if err != nil {
//		return nil, err
//	}
//	cache.Unref()
//	return &pebbledb{
//		db:     db,
//		ro:     ro,
//		wo:     wo,
//		syncwo: syncwo,
//	}, nil
//}
//
//func isNewRun(dir string) bool {
//	fp := filepath.Join(dir, currentDBFilename)
//	if _, err := os.Stat(fp); os.IsNotExist(err) {
//		return true
//	}
//	return false
//}
//
//func getNewRandomDBDirName(dir string) string {
//	part := "%d_%d"
//	rn := rand.Uint64()
//	ct := time.Now().UnixNano()
//	return filepath.Join(dir, fmt.Sprintf(part, rn, ct))
//}
//
//func replaceCurrentDBFile(dir string) error {
//	fp := filepath.Join(dir, currentDBFilename)
//	tmpFp := filepath.Join(dir, updatingDBFilename)
//	if err := os.Rename(tmpFp, fp); err != nil {
//		return err
//	}
//	return syncDir(dir)
//}
//
//func saveCurrentDBDirName(dir string, dbdir string) error {
//	h := md5.New()
//	if _, err := h.Write([]byte(dbdir)); err != nil {
//		return err
//	}
//	fp := filepath.Join(dir, updatingDBFilename)
//	f, err := os.Create(fp)
//	if err != nil {
//		return err
//	}
//	defer func() {
//		if err := f.Close(); err != nil {
//			panic(err)
//		}
//		if err := syncDir(dir); err != nil {
//			panic(err)
//		}
//	}()
//	if _, err := f.Write(h.Sum(nil)[:8]); err != nil {
//		return err
//	}
//	if _, err := f.Write([]byte(dbdir)); err != nil {
//		return err
//	}
//	if err := f.Sync(); err != nil {
//		return err
//	}
//	return nil
//}
//
//func getCurrentDBDirName(dir string) (string, error) {
//	fp := filepath.Join(dir, currentDBFilename)
//	f, err := os.OpenFile(fp, os.O_RDONLY, 0755)
//	if err != nil {
//		return "", err
//	}
//	defer func() {
//		if err := f.Close(); err != nil {
//			panic(err)
//		}
//	}()
//	data, err := ioutil.ReadAll(f)
//	if err != nil {
//		return "", err
//	}
//	if len(data) <= 8 {
//		panic("corrupted content")
//	}
//	crc := data[:8]
//	content := data[8:]
//	h := md5.New()
//	if _, err := h.Write(content); err != nil {
//		return "", err
//	}
//	if !bytes.Equal(crc, h.Sum(nil)[:8]) {
//		panic("corrupted content with not matched crc")
//	}
//	return string(content), nil
//}
//
//func createNodeDataDir(dir string) error {
//	if err := os.MkdirAll(dir, 0755); err != nil {
//		return err
//	}
//	return syncDir(filepath.Dir(dir))
//}
//
//func cleanupNodeDataDir(dir string) error {
//	os.RemoveAll(filepath.Join(dir, updatingDBFilename))
//	dbdir, err := getCurrentDBDirName(dir)
//	if err != nil {
//		return err
//	}
//	files, err := ioutil.ReadDir(dir)
//	if err != nil {
//		return err
//	}
//	for _, fi := range files {
//		if !fi.IsDir() {
//			continue
//		}
//		toDelete := filepath.Join(dir, fi.Name())
//		if toDelete != dbdir {
//			if err := os.RemoveAll(toDelete); err != nil {
//				return err
//			}
//		}
//	}
//	return nil
//}
//
//// OptimizationLevel 优化级别
//type OptimizationLevel int
//
//const (
//	// OptDefault - 默认配置（保守，最安全）
//	OptDefault OptimizationLevel = iota
//	// OptBalanced - 平衡配置（推荐生产环境）
//	OptBalanced
//	// OptPerformance - 性能优先（牺牲部分持久性保证）
//	OptPerformance
//	// OptAggressive - 激进配置（仅测试环境）
//	OptAggressive
//)
//
//// String 返回优化级别的字符串表示
//func (o OptimizationLevel) String() string {
//	switch o {
//	case OptDefault:
//		return "Default"
//	case OptBalanced:
//		return "Balanced"
//	case OptPerformance:
//		return "Performance"
//	case OptAggressive:
//		return "Aggressive"
//	default:
//		return "Unknown"
//	}
//}
//
//// DiskKV is a state machine that implements the IOnDiskStateMachine interface
//type DiskKV struct {
//	clusterID   uint64
//	nodeID      uint64
//	lastApplied uint64
//	dataDir     string            // 自定义数据目录
//	optLevel    OptimizationLevel // 优化级别
//	db          unsafe.Pointer
//	closed      bool
//	aborted     bool
//
//	// 🔥 优化：复用缓冲区，减少内存分配
//	kvData          KVData  // 复用的 KVData 对象
//	appliedIndexBuf [8]byte // 复用的 appliedIndex 缓冲区
//}
//
//// NewDiskKV creates a new disk kv state machine
//func NewDiskKV(clusterID uint64, nodeID uint64) sm.IOnDiskStateMachine {
//	return &DiskKV{
//		clusterID: clusterID,
//		nodeID:    nodeID,
//		optLevel:  OptBalanced, // 默认使用Balanced配置
//	}
//}
//
//// NewDiskKVWithOpt creates a new disk kv state machine with optimization level
//func NewDiskKVWithOpt(clusterID uint64, nodeID uint64, optLevel OptimizationLevel) sm.IOnDiskStateMachine {
//	return &DiskKV{
//		clusterID: clusterID,
//		nodeID:    nodeID,
//		optLevel:  optLevel,
//	}
//}
//
//// SetDataDir sets the data directory
//func (d *DiskKV) SetDataDir(dir string) {
//	d.dataDir = dir
//}
//
//// SetOptimizationLevel 设置优化级别
//func (d *DiskKV) SetOptimizationLevel(level OptimizationLevel) {
//	d.optLevel = level
//}
//
//func (d *DiskKV) getNodeDBDirName() string {
//	if d.dataDir != "" {
//		return d.dataDir
//	}
//	// 默认目录
//	part := fmt.Sprintf("%d_%d", d.clusterID, d.nodeID)
//	return filepath.Join("rundata", "diskkv", part)
//}
//
//func (d *DiskKV) queryAppliedIndex(db *pebbledb) (uint64, error) {
//	val, closer, err := db.db.Get([]byte(appliedIndexKey))
//	if err != nil && err != pebble.ErrNotFound {
//		return 0, err
//	}
//	defer func() {
//		if closer != nil {
//			closer.Close()
//		}
//	}()
//	if len(val) == 0 {
//		return 0, nil
//	}
//	return binary.LittleEndian.Uint64(val), nil
//}
//
//// Open opens the state machine and return the index of the last Raft Log entry
//func (d *DiskKV) Open(stopc <-chan struct{}) (uint64, error) {
//	dir := d.getNodeDBDirName()
//	if err := createNodeDataDir(dir); err != nil {
//		panic(err)
//	}
//	var dbdir string
//	if !isNewRun(dir) {
//		if err := cleanupNodeDataDir(dir); err != nil {
//			return 0, err
//		}
//		var err error
//		dbdir, err = getCurrentDBDirName(dir)
//		if err != nil {
//			return 0, err
//		}
//		if _, err := os.Stat(dbdir); err != nil {
//			if os.IsNotExist(err) {
//				panic("db dir unexpectedly deleted")
//			}
//		}
//	} else {
//		dbdir = getNewRandomDBDirName(dir)
//		if err := saveCurrentDBDirName(dir, dbdir); err != nil {
//			return 0, err
//		}
//		if err := replaceCurrentDBFile(dir); err != nil {
//			return 0, err
//		}
//	}
//	// 使用配置的优化级别创建DB
//	db, err := createDB(dbdir, d.optLevel)
//	if err != nil {
//		return 0, err
//	}
//	atomic.SwapPointer(&d.db, unsafe.Pointer(db))
//	appliedIndex, err := d.queryAppliedIndex(db)
//	if err != nil {
//		panic(err)
//	}
//	d.lastApplied = appliedIndex
//	return appliedIndex, nil
//}
//
//// Lookup queries the state machine
//func (d *DiskKV) Lookup(key interface{}) (interface{}, error) {
//	db := (*pebbledb)(atomic.LoadPointer(&d.db))
//	if db != nil {
//		v, err := db.lookup(key.([]byte))
//		if err == nil && d.closed {
//			panic("lookup returned valid result when DiskKV is already closed")
//		}
//		if err == pebble.ErrNotFound {
//			return v, nil
//		}
//		return v, err
//	}
//	return nil, errors.New("db closed")
//}
//
//// Update updates the state machine
//func (d *DiskKV) Update(ents []sm.Entry) ([]sm.Entry, error) {
//	if d.aborted {
//		panic("update() called after abort set to true")
//	}
//	if d.closed {
//		panic("update called after Close()")
//	}
//	db := (*pebbledb)(atomic.LoadPointer(&d.db))
//
//	wb := db.db.NewBatch()
//	defer wb.Close()
//
//	// 批量处理所有entries
//	for idx, e := range ents {
//		// 🔥 优化：复用 d.kvData 对象，避免频繁内存分配
//		d.kvData.Key = ""
//		d.kvData.Val = ""
//
//		if err := json.Unmarshal(e.Cmd, &d.kvData); err != nil {
//			panic(err)
//		}
//		wb.Set([]byte(d.kvData.Key), []byte(d.kvData.Val), db.wo)
//		ents[idx].Result = sm.Result{Value: uint64(len(ents[idx].Cmd))}
//	}
//
//	// 🔥 优化：复用 appliedIndexBuf，避免分配
//	binary.LittleEndian.PutUint64(d.appliedIndexBuf[:], ents[len(ents)-1].Index)
//	wb.Set([]byte(appliedIndexKey), d.appliedIndexBuf[:], db.wo)
//
//	if err := db.db.Apply(wb, db.syncwo); err != nil {
//		return nil, err
//	}
//
//	if d.lastApplied >= ents[len(ents)-1].Index {
//		panic("lastApplied not moving forward")
//	}
//	d.lastApplied = ents[len(ents)-1].Index
//
//	return ents, nil
//}
//
//// Sync synchronizes all in-core state
//func (d *DiskKV) Sync() error {
//	return nil
//}
//
//type diskKVCtx struct {
//	db       *pebbledb
//	snapshot *pebble.Snapshot
//}
//
//// PrepareSnapshot prepares snapshotting
//func (d *DiskKV) PrepareSnapshot() (interface{}, error) {
//	if d.closed {
//		panic("prepare snapshot called after Close()")
//	}
//	if d.aborted {
//		panic("prepare snapshot called after abort")
//	}
//	db := (*pebbledb)(atomic.LoadPointer(&d.db))
//	return &diskKVCtx{
//		db:       db,
//		snapshot: db.db.NewSnapshot(),
//	}, nil
//}
//
//func iteratorIsValid(iter *pebble.Iterator) bool {
//	return iter.Valid()
//}
//
//func (d *DiskKV) saveToWriter(db *pebbledb,
//	ss *pebble.Snapshot, w io.Writer) error {
//	iter := ss.NewIter(db.ro)
//	defer iter.Close()
//	values := make([]*KVData, 0)
//	for iter.First(); iteratorIsValid(iter); iter.Next() {
//		kv := &KVData{
//			Key: string(iter.Key()),
//			Val: string(iter.Value()),
//		}
//		values = append(values, kv)
//	}
//	count := uint64(len(values))
//	sz := make([]byte, 8)
//	binary.LittleEndian.PutUint64(sz, count)
//	if _, err := w.Write(sz); err != nil {
//		return err
//	}
//	for _, dataKv := range values {
//		data, err := json.Marshal(dataKv)
//		if err != nil {
//			panic(err)
//		}
//		binary.LittleEndian.PutUint64(sz, uint64(len(data)))
//		if _, err := w.Write(sz); err != nil {
//			return err
//		}
//		if _, err := w.Write(data); err != nil {
//			return err
//		}
//	}
//	return nil
//}
//
//// SaveSnapshot saves the state machine state
//func (d *DiskKV) SaveSnapshot(ctx interface{},
//	w io.Writer, done <-chan struct{}) error {
//	if d.closed {
//		panic("prepare snapshot called after Close()")
//	}
//	if d.aborted {
//		panic("prepare snapshot called after abort")
//	}
//	ctxdata := ctx.(*diskKVCtx)
//	db := ctxdata.db
//	db.mu.RLock()
//	defer db.mu.RUnlock()
//	ss := ctxdata.snapshot
//	defer ss.Close()
//	return d.saveToWriter(db, ss, w)
//}
//
//// RecoverFromSnapshot recovers the state machine state from snapshot
//func (d *DiskKV) RecoverFromSnapshot(r io.Reader,
//	done <-chan struct{}) error {
//	if d.closed {
//		panic("recover from snapshot called after Close()")
//	}
//	dir := d.getNodeDBDirName()
//	dbdir := getNewRandomDBDirName(dir)
//	oldDirName, err := getCurrentDBDirName(dir)
//	if err != nil {
//		return err
//	}
//	// 恢复快照时使用配置的优化级别
//	db, err := createDB(dbdir, d.optLevel)
//	if err != nil {
//		return err
//	}
//	sz := make([]byte, 8)
//	if _, err := io.ReadFull(r, sz); err != nil {
//		return err
//	}
//	total := binary.LittleEndian.Uint64(sz)
//	wb := db.db.NewBatch()
//	defer wb.Close()
//	for i := uint64(0); i < total; i++ {
//		if _, err := io.ReadFull(r, sz); err != nil {
//			return err
//		}
//		toRead := binary.LittleEndian.Uint64(sz)
//		data := make([]byte, toRead)
//		if _, err := io.ReadFull(r, data); err != nil {
//			return err
//		}
//		dataKv := &KVData{}
//		if err := json.Unmarshal(data, dataKv); err != nil {
//			panic(err)
//		}
//		wb.Set([]byte(dataKv.Key), []byte(dataKv.Val), db.wo)
//	}
//	if err := db.db.Apply(wb, db.syncwo); err != nil {
//		return err
//	}
//	if err := saveCurrentDBDirName(dir, dbdir); err != nil {
//		return err
//	}
//	if err := replaceCurrentDBFile(dir); err != nil {
//		return err
//	}
//	newLastApplied, err := d.queryAppliedIndex(db)
//	if err != nil {
//		panic(err)
//	}
//	if d.lastApplied > newLastApplied {
//		panic("last applied not moving forward")
//	}
//	d.lastApplied = newLastApplied
//	old := (*pebbledb)(atomic.SwapPointer(&d.db, unsafe.Pointer(db)))
//	if old != nil {
//		old.close()
//	}
//	parent := filepath.Dir(oldDirName)
//	if err := os.RemoveAll(oldDirName); err != nil {
//		return err
//	}
//	return syncDir(parent)
//}
//
//// Close closes the state machine
//func (d *DiskKV) Close() error {
//	db := (*pebbledb)(atomic.SwapPointer(&d.db, unsafe.Pointer(nil)))
//	if db != nil {
//		d.closed = true
//		db.close()
//	} else {
//		if d.closed {
//			panic("close called twice")
//		}
//	}
//	return nil
//}
