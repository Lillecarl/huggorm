"""Load the build-time gRPC schema emitted by huggorm-generated.

The descriptor set and nothing else. Every other build-time fact is
an emitted constant in `huggorm_generated._policy`.
"""

from __future__ import annotations

import hashlib
import pathlib
from dataclasses import dataclass
from typing import Any

import grpclib.const
from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

import huggorm_generated
from huggorm_generated._policy import PKG as _PKG


def _pkg_dir() -> pathlib.Path:
    return pathlib.Path(huggorm_generated.__file__).parent


def schema_digest() -> str:
    """What a client and a server must share before any other call.

    Field numbers are positional, so two builds can number one field
    differently, and proto3 decodes a wrong field as a default rather
    than an error. Bind compares this digest and refuses a different
    one (huggorm#22)."""
    return hashlib.sha256(
        (_pkg_dir() / "grpc_schema.pb").read_bytes()).hexdigest()


def load_pool() -> descriptor_pool.DescriptorPool:
    # grpc_schema.pb holds a FileDescriptorSet; unwrap it into its
    # FileDescriptorProtos before adding to the pool.
    # protobuf's shipped stubs do not describe the generated
    # descriptor module, so these three calls are opaque to a
    # typechecker. The shapes are fixed by the protobuf spec.
    fds = descriptor_pb2.FileDescriptorSet.FromString(  # type: ignore[attr-defined]
        (_pkg_dir() / "grpc_schema.pb").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for file_dp in fds.file:
        pool.Add(file_dp)  # type: ignore[no-untyped-call]
    return pool


# The protobuf package every message and service sits in. DERIVED,
# not written here: grpc_schema decides it, so a rename reaches this
# file the way it reaches every other consumer (huggorm#45).
#
# Re-exported rather than imported at each use site, because this
# module is where every caller already looks for schema facts. It is
# an emitted constant, so no other generator can supply a different
# one and nothing needs to check it at load time.
PKG = _PKG


def session(name: str) -> str:
    """The gRPC path of one hand-written Session rpc."""
    return f"/{PKG}.Session/{name}"


@dataclass(frozen=True)
class Rpc:
    """One rpc as the schema states it.

    Read by path on both sides, so neither restates a message name: a
    request built as the wrong message would decode as default fields,
    not fail."""

    req: Any
    resp: Any
    cardinality: grpclib.const.Cardinality


class Rpcs:
    """Every rpc in one pool, by gRPC path, each read once."""

    def __init__(self, pool: descriptor_pool.DescriptorPool) -> None:
        self.pool = pool
        self._read: dict[str, Rpc] = {}

    def __getitem__(self, path: str) -> Rpc:
        if (found := self._read.get(path)) is not None:
            return found
        m = self.pool.FindMethodByName(  # type: ignore[no-untyped-call]
            path.lstrip("/").replace("/", "."))
        found = self._read[path] = Rpc(
            message_factory.GetMessageClass(m.input_type),  # type: ignore[no-untyped-call]
            message_factory.GetMessageClass(m.output_type),  # type: ignore[no-untyped-call]
            grpclib.const.Cardinality.UNARY_STREAM if m.server_streaming
            else grpclib.const.Cardinality.UNARY_UNARY)
        return found
