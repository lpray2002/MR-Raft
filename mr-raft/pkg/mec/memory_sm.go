// Copyright 2017-2025 Based on Dragonboat examples
//
// MemoryKV - Pure in-memory state machine for performance testing
// 纯内存状态机，用于性能测试（无磁盘I/O）

package mec

import (
	"encoding/json"
	"errors"
	"io"
	"sync"

	sm "github.com/lni/dragonboat/v3/statemachine"
)

// MemoryKV 纯内存KV状态机
type MemoryKV struct {
	clusterID   uint64
	nodeID      uint64
	data        map[string]string // 内存存储
	vers        map[string]uint64 // 版本（仅 Ver>0 的写入）
	mu          sync.RWMutex      // 读写锁
	lastApplied uint64            // 最后应用的索引
}

// NewMemoryKV 创建纯内存状态机
func NewMemoryKV(clusterID uint64, nodeID uint64) sm.IStateMachine {
	return &MemoryKV{
		clusterID: clusterID,
		nodeID:    nodeID,
		data:      make(map[string]string),
		vers:      make(map[string]uint64),
	}
}

// Update 更新状态机（纯内存操作）。
// 当命令携带 Ver>0 时执行按版本的 last-writer-wins：旧版本的写入被忽略。
func (m *MemoryKV) Update(data []byte) (sm.Result, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	var kv KVData
	if err := json.Unmarshal(data, &kv); err != nil {
		return sm.Result{}, err
	}
	if kv.Ver > 0 {
		if cur, ok := m.vers[kv.Key]; ok && cur >= kv.Ver {
			return sm.Result{Value: 0}, nil // stale write ignored
		}
		m.vers[kv.Key] = kv.Ver
	}
	m.data[kv.Key] = kv.Val
	return sm.Result{Value: uint64(len(m.data))}, nil
}

// Lookup 查询数据（只读操作）
func (m *MemoryKV) Lookup(query interface{}) (interface{}, error) {
	m.mu.RLock()
	defer m.mu.RUnlock()

	// Full-state scan (used by the CM for state handoff between regions).
	if sq, ok := query.(*ScanQuery); ok {
		out := make(map[string]string, len(m.data))
		vers := make(map[string]uint64, len(m.vers))
		for k, v := range m.data {
			if sq.Limit > 0 && len(out) >= sq.Limit {
				break
			}
			out[k] = v
			if ver, ok := m.vers[k]; ok {
				vers[k] = ver
			}
		}
		return &ScanResult{KVs: out, Vers: vers}, nil
	}

	if kq, ok := query.(*KeyQuery); ok {
		val, exists := m.data[kq.Key]
		return &KVResult{Found: exists, Val: val, Ver: m.vers[kq.Key]}, nil
	}

	key, ok := query.(string)
	if !ok {
		if b, isBytes := query.([]byte); isBytes {
			key = string(b)
		} else {
			return nil, errors.New("query must be a string")
		}
	}

	val, exists := m.data[key]
	if !exists {
		return nil, errors.New("key not found")
	}
	return val, nil
}

// SaveSnapshot 保存快照（纯内存，非常快）
func (m *MemoryKV) SaveSnapshot(w io.Writer, fc sm.ISnapshotFileCollection, done <-chan struct{}) error {
	m.mu.RLock()
	defer m.mu.RUnlock()

	snapshot := &MemorySnapshot{
		Data:        m.data,
		Vers:        m.vers,
		LastApplied: m.lastApplied,
	}
	encoder := json.NewEncoder(w)
	return encoder.Encode(snapshot)
}

// RecoverFromSnapshot 从快照恢复（纯内存，非常快）
func (m *MemoryKV) RecoverFromSnapshot(r io.Reader, files []sm.SnapshotFile, done <-chan struct{}) error {
	m.mu.Lock()
	defer m.mu.Unlock()

	var snapshot MemorySnapshot
	decoder := json.NewDecoder(r)
	if err := decoder.Decode(&snapshot); err != nil {
		return err
	}
	m.data = snapshot.Data
	if snapshot.Vers != nil {
		m.vers = snapshot.Vers
	} else {
		m.vers = make(map[string]uint64)
	}
	m.lastApplied = snapshot.LastApplied
	return nil
}

// Close 关闭状态机（无需清理）
func (m *MemoryKV) Close() error {
	return nil
}

// MemorySnapshot 内存快照结构
type MemorySnapshot struct {
	Data        map[string]string `json:"data"`
	Vers        map[string]uint64 `json:"vers,omitempty"`
	LastApplied uint64            `json:"last_applied"`
}
