package main

import (
	"context"
	"time"

	dragonboat "github.com/lni/dragonboat/v3"
	"github.com/lni/dragonboat/v3/config"
	"github.com/lni/dragonboat/v3/raftio"
	pb "github.com/lni/dragonboat/v3/raftpb"
)

// DelayTransportFactory implements config.TransportFactory.
// It wraps the default TCP transport to inject network delay on every
// Raft message send, simulating real-world inter-node latency.
type DelayTransportFactory struct {
	Delay time.Duration
}

func (f *DelayTransportFactory) Create(nhConfig config.NodeHostConfig,
	handler raftio.MessageHandler, chunkHandler raftio.ChunkHandler) raftio.ITransport {
	inner := dragonboat.NewTCPTransport(nhConfig, handler, chunkHandler)
	if f.Delay <= 0 {
		return inner
	}
	return &DelayTransport{inner: inner, delay: f.Delay}
}

func (f *DelayTransportFactory) Validate(addr string) bool {
	return config.IsValidAddress(addr)
}

// DelayTransport wraps ITransport to inject delay on message sends.
type DelayTransport struct {
	inner raftio.ITransport
	delay time.Duration
}

func (t *DelayTransport) Name() string {
	return "delay-" + t.inner.Name()
}

func (t *DelayTransport) Start() error {
	return t.inner.Start()
}

func (t *DelayTransport) Stop() {
	t.inner.Stop()
}

func (t *DelayTransport) GetConnection(ctx context.Context, target string) (raftio.IConnection, error) {
	conn, err := t.inner.GetConnection(ctx, target)
	if err != nil {
		return nil, err
	}
	return &delayConnection{inner: conn, delay: t.delay}, nil
}

func (t *DelayTransport) GetSnapshotConnection(ctx context.Context, target string) (raftio.ISnapshotConnection, error) {
	return t.inner.GetSnapshotConnection(ctx, target)
}

// delayConnection wraps IConnection to add delay before sending messages.
type delayConnection struct {
	inner raftio.IConnection
	delay time.Duration
}

func (c *delayConnection) Close() {
	c.inner.Close()
}

func (c *delayConnection) SendMessageBatch(batch pb.MessageBatch) error {
	if c.delay > 0 {
		time.Sleep(c.delay)
	}
	return c.inner.SendMessageBatch(batch)
}
