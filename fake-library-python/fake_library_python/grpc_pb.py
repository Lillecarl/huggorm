"""Load the build-time gRPC schema emitted by fake-library-generated."""

import json
import pathlib

from google.protobuf import descriptor_pb2, descriptor_pool

import fake_library_generated

PKG = "nixmock.v1"


def _pkg_dir() -> pathlib.Path:
    return pathlib.Path(fake_library_generated.__file__).parent


def load_pool() -> descriptor_pool.DescriptorPool:
    # grpc_schema.pb holds a FileDescriptorSet; unwrap it into its
    # FileDescriptorProtos before adding to the pool.
    fds = descriptor_pb2.FileDescriptorSet.FromString(
        (_pkg_dir() / "grpc_schema.pb").read_bytes())
    pool = descriptor_pool.DescriptorPool()
    for file_dp in fds.file:
        pool.Add(file_dp)
    return pool


def load_manifest() -> dict:
    return json.loads((_pkg_dir() / "manifest.json").read_text())
