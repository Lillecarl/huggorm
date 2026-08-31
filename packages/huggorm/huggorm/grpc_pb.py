"""Load the build-time gRPC schema emitted by huggorm-generated."""

import json
import pathlib
from typing import Any

from google.protobuf import descriptor_pb2, descriptor_pool

import huggorm_generated
from huggorm_generated._policy import PKG as _PKG
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
    """The build's manifest, for a TEST to assert against.

    Nothing in this library reads it any more. The server, the
    client, the codec and the fault codec each took a table out of it
    at run time; all of those tables are emitted Python now, in
    `huggorm_generated._policy`, so the JSON has no reader left except
    the suite - which uses it as a second description of the surface
    to hold the emitted one against.

    `check_manifest` goes with the JSON when the suite stops needing
    it. It exists only because a file can come from another
    generator: *"it would answer wrong, one lookup at a time"*. An
    emitted module ships with the code that reads it."""
    manifest: dict[str, Any] = json.loads(
        (_pkg_dir() / "manifest.json").read_text())
    check_manifest(manifest)
    return manifest


# The protobuf package every message and service sits in. DERIVED,
# not written here: grpc_schema decides it, so a rename reaches this
# file the way it reaches every other consumer. It used to be a second
# copy of the string, and a rename had to find it (tasks/045).
#
# Re-exported rather than imported at each use site, because this
# module is where every caller already looks for schema facts. It came
# from `manifest.json`, through a `load_manifest` that ran
# `check_manifest` first - a check that existed only because JSON
# could come from another generator. An emitted constant cannot.
PKG = _PKG
