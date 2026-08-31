"""Load the build-time gRPC schema emitted by huggorm-generated.

The descriptor set and nothing else. `load_manifest` lived here and
is gone: no code in this library reads `manifest.json` any more, so
the one reader left - the suite, which uses it as an enumeration of
what the build decided - keeps its own (065).
"""

import pathlib

from google.protobuf import descriptor_pb2, descriptor_pool

import huggorm_generated
from huggorm_generated._policy import PKG as _PKG


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
