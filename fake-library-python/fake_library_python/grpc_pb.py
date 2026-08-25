"""Load the build-time gRPC schema emitted by fake-library-generated."""

import json
import pathlib
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool

import fake_library_generated
from fake_library_generated._wiretypes import check_manifest

PKG = "nixmock.v1"


def _pkg_dir() -> pathlib.Path:
    return pathlib.Path(fake_library_generated.__file__).parent


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


def load_manifest() -> dict[str, Any]:
    manifest: dict[str, Any] = json.loads(
        (_pkg_dir() / "manifest.json").read_text())
    # The server and the client read this to learn every type, policy
    # and rpc name they use. A manifest from another generator would
    # not fail here - it would answer wrong, one lookup at a time.
    check_manifest(manifest)
    return manifest
