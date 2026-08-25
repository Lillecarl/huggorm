"""
The first REAL Nix type (tasks/015).

nix::StorePath is the smallest thing that proves the chain end to end:
pkg-config linkage against the split nix-store component, a namespaced
C++ class, a constructor that validates and throws from libstore, and
accessors returning views into the object's own storage.

It stands beside the mock's StorePath rather than replacing it.
"""

import copy
from typing import Any

import pytest

from cythonix import grpc_pb
from cythonix.wire import WireCodec
from cythonix_bindings import StorePath

HELLO = "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"


def test_a_real_store_path_parses() -> None:
    p = StorePath(HELLO)
    assert p.to_string() == HELLO
    assert p.name() == "hello-2.12.1"
    assert p.hash_part() == "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y"
    assert len(p.hash_part()) == 32, "HashLen, in base-32 characters"


def test_derivations_are_recognised() -> None:
    assert not StorePath(HELLO).is_derivation()
    assert StorePath(HELLO + ".drv").is_derivation()


def test_validation_comes_from_libstore() -> None:
    """The whole reason to bind the real thing rather than reimplement
    it. Nothing in this repo knows what makes a store path valid."""
    with pytest.raises(RuntimeError, match="too short to be a valid store path"):
        StorePath("not-a-store-path")
    with pytest.raises(RuntimeError):
        StorePath("0" * 32 + "-bad name with spaces")


def test_accessors_do_not_hand_back_views() -> None:
    """Every accessor returns a string_view INTO the object's baseName.
    A view outliving its owner is a dangling pointer, not an exception,
    so the binding copies before anything reaches Python."""
    name = StorePath(HELLO).name()  # the path itself is now garbage
    assert name == "hello-2.12.1"


def test_a_real_path_copies() -> None:
    p = StorePath(HELLO)
    assert copy.copy(p).to_string() == HELLO
    # Immutable, so a deep copy is a copy.
    assert copy.deepcopy(p).to_string() == HELLO


def test_a_real_path_crosses_the_wire() -> None:
    """It is a wire-value like any other: the codec builds its message
    from the _wire_fields the binding declares, and rebuilds it on the
    far side through _from_parts. No layer above the binding knows the
    type exists."""
    manifest = grpc_pb.load_manifest()
    proto = manifest["wrappers"]["StorePath"]
    assert proto["binds"] == "CStorePath"
    assert proto["wire"] == "value"

    codec = WireCodec(manifest)
    msg = _message(proto["message"])()
    codec.value_to_msg("StorePath", StorePath(HELLO), msg)
    assert msg.base_name == HELLO

    back = codec.value_from_msg("StorePath", msg)
    assert isinstance(back, StorePath)
    assert back.to_string() == HELLO


def _message(name: str) -> Any:
    # protobuf ships no stubs for its own descriptor machinery, so
    # these calls are opaque to a typechecker. The shapes are fixed by
    # the protobuf spec; grpc_pb.py carries the same note.
    from google.protobuf import message_factory

    pool = grpc_pb.load_pool()
    return message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
        pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
            f"{grpc_pb.PKG}.{name}"))
