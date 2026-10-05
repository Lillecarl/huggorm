"""Load the build-time gRPC schema emitted by huggorm-generated.

The descriptor set and nothing else. Every other build-time fact is
an emitted constant in `huggorm_generated._policy`.
"""

from __future__ import annotations

import hashlib
import pathlib

from google.protobuf import descriptor_pb2, descriptor_pool

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
