package mec

import (
	"encoding/json"
	"errors"
	"fmt"

	sm "github.com/lni/dragonboat/v3/statemachine"
)

// Transaction is immutable for a given ID. The fixed participant set is also
// retained by the coordinator across retries and topology changes.
type Transaction struct {
	ID           string   `json:"id"`
	Coordinator  string   `json:"coordinator"`
	Participants []uint64 `json:"participants"`
	Write        KVData   `json:"write"`
}

type TxnState string

const (
	TxnPrepared  TxnState = "prepared"
	TxnCommitted TxnState = "committed"
	TxnAborted   TxnState = "aborted"
	TxnUnknown   TxnState = "unknown"
	TxnRejected  TxnState = "rejected"
	OpPrepare             = "prepare"
	OpCommit              = "commit"
	OpAbort               = "abort"
)

// TxnCommand is carried by the existing Raft proposal API. A legacy KV command
// has no txn_op; clients cannot inject this envelope through the public Propose
// RPC, which decodes its data into KVData first.
type TxnCommand struct {
	Op          string      `json:"txn_op"`
	Transaction Transaction `json:"transaction"`
}

type TxnRecord struct {
	Transaction Transaction `json:"transaction"`
	State       TxnState    `json:"state"`
}

type TxnReply struct {
	State TxnState `json:"state"`
	Error string   `json:"error,omitempty"`
}

// These queries are internal recovery views. Ordinary key reads and scans do
// not expose pending values; the records here include the immutable write.
type TxnQuery struct{ ID string }
type PendingTxnQuery struct{}
type PendingTxnResult struct{ Transactions []TxnRecord }

var ErrTxnPending = errors.New("transaction pending")

type PendingTxnError struct{ ID string }

func (e *PendingTxnError) Error() string { return fmt.Sprintf("transaction %s pending", e.ID) }
func (e *PendingTxnError) Unwrap() error { return ErrTxnPending }

func EncodeTxnReply(state TxnState, message string) sm.Result {
	data, _ := json.Marshal(TxnReply{State: state, Error: message})
	return sm.Result{Data: data}
}

func DecodeTxnReply(result sm.Result) (TxnReply, error) {
	var reply TxnReply
	if err := json.Unmarshal(result.Data, &reply); err != nil {
		return reply, fmt.Errorf("invalid transaction reply: %w", err)
	}
	if reply.State == "" {
		return reply, errors.New("missing transaction state")
	}
	return reply, nil
}
