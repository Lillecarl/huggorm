"""Load the build-time gRPC schema emitted by huggorm-generated."""

import json
import pathlib
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool

import huggorm_generated
from huggorm_generated._wiretypes import check_manifest


def _pkg_dir() -> pathlib.Path:
    return pathlib.Path(huggorm_generated.__file__).parent


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


# The protobuf package every message and service sits in. DERIVED, not
# written here: grpc_schema stamps it into the manifest in annotate(),
# so a rename reaches this file the way it reaches every other
# consumer. It used to be a second copy of the string, and a rename
# had to find it (tasks/045).
#
# Read once, at import, because the manifest is read once anyway and
# because every caller wants a constant rather than a function call
# per rpc path.
PKG: str = str(load_manifest()["package"])
