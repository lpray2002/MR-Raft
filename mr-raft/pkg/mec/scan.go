// Package mec: full-state scan support used by the Cluster Manager (CM) for
// state handoff when consistency regions are merged/replaced.
//
// Both MemoryKV and DiskKV accept a *ScanQuery value in Lookup(); the result is
// a snapshot of the local state machine (internal bookkeeping keys such as the
// applied-index key and version keys are excluded from KVs).
package mec

// ScanQuery asks a state machine to dump all of its user-visible KV pairs.
// Limit == 0 means no limit.
type ScanQuery struct {
	Limit int
}

// ScanResult is the value returned from Lookup for a *ScanQuery.
type ScanResult struct {
	KVs  map[string]string
	Vers map[string]uint64 // version per key (only keys written with Ver > 0)
}

// KeyQuery asks for one key together with its version. A node that belongs to
// several regions uses the version to return the freshest committed copy.
type KeyQuery struct {
	Key string
}

// KVResult is the value returned from Lookup for a *KeyQuery.
type KVResult struct {
	Found bool
	Val   string
	Ver   uint64
}
