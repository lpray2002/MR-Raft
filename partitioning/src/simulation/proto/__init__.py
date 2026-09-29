"""
gRPC stubs generated from multi-region-raft/proto/raft_service.proto.

Regenerate with (from the multi-region-raft repo root):
    python -m grpc_tools.protoc -Iproto \
        --python_out=<this dir> --grpc_python_out=<this dir> proto/raft_service.proto

The generated *_grpc.py uses an absolute ``import raft_service_pb2``; we put this
directory on sys.path so that both ``from .proto import raft_service_pb2`` and the
generated absolute import resolve to the same module object.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import raft_service_pb2  # noqa: E402
import raft_service_pb2_grpc  # noqa: E402

__all__ = ["raft_service_pb2", "raft_service_pb2_grpc"]
