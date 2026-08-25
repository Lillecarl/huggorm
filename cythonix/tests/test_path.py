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
from cythonix_bindings.errors import BadStorePath, NixError

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
    with pytest.raises(BadStorePath, match="too short to be a valid store path"):
        StorePath("not-a-store-path")
    with pytest.raises(BadStorePath):
        StorePath("0" * 32 + "-bad name with spaces")


def test_a_nix_error_keeps_its_type() -> None:
    """Cython's bare `except +` maps anything it does not recognise
    onto RuntimeError, which loses every distinction libstore drew.
    `except +translate_nix_error` is the hook that keeps them, and the
    hierarchy mirrors nix's own so catching the base still works."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    assert isinstance(caught.value, BadStorePath)
    assert not isinstance(caught.value, RuntimeError), "the old behaviour"


def test_error_messages_carry_no_terminal_escapes() -> None:
    """libstore writes its messages in colour whether or not anything
    is a terminal, so what() comes back holding escape codes. They are
    stripped at the boundary: they are wrong in a traceback and wrong
    over the wire, and every reader downstream would have to know."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    assert "\x1b" not in str(caught.value), repr(str(caught.value))


def test_the_colour_is_kept_beside_the_plain_message() -> None:
    """It exists so an error can be PRINTED to a terminal, which is
    the one place it is useful. Stripping it at the boundary and
    nowhere else would take that from every caller who has a tty."""
    with pytest.raises(NixError) as caught:
        StorePath("nope")
    err = caught.value
    assert "\x1b[" in err.colored, repr(err.colored)
    assert err.message == str(err)
    # Same message, one dressed and one not.
    assert "too short to be a valid store path" in err.colored
    assert "too short to be a valid store path" in err.message


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
