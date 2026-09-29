package dragonboat

import (
	"github.com/lni/dragonboat/v3/config"
	"github.com/lni/dragonboat/v3/internal/transport"
	"github.com/lni/dragonboat/v3/raftio"
)

// NewTCPTransport creates a default TCP transport instance.
// Exported for use by custom TransportFactory wrappers.
func NewTCPTransport(nhConfig config.NodeHostConfig,
	requestHandler raftio.MessageHandler,
	chunkHandler raftio.ChunkHandler) raftio.ITransport {
	return transport.NewTCPTransport(nhConfig, requestHandler, chunkHandler)
}
