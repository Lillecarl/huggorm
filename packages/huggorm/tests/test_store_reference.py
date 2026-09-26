"""A store URI as libstore reads it (`store-reference.cc`).

Each case is one branch of `StoreReference::parse`, and the arm it
lands in is the fact under test: `daemon` and `daemon?x=y` are
different arms upstream, and a caller must be told which it got.
"""

import pytest

from huggorm_bindings import (
    StoreReferenceAuto,
    StoreReferenceDaemon,
    StoreReferenceLocal,
    StoreReferenceSpecified,
    parse_store_reference,
)
from huggorm_bindings.errors import UsageError


@pytest.mark.parametrize("uri", ["auto", ""])
def test_auto_and_the_empty_string_are_auto(uri: str) -> None:
    assert isinstance(parse_store_reference(uri).variant(), StoreReferenceAuto)


def test_daemon_and_local_are_arms_of_their_own() -> None:
    assert isinstance(parse_store_reference("daemon").variant(), StoreReferenceDaemon)
    assert isinstance(parse_store_reference("local").variant(), StoreReferenceLocal)


def test_a_parameter_turns_daemon_into_a_unix_uri() -> None:
    """`parse` returns `Daemon{}` only when there are no parameters."""
    ref = parse_store_reference("daemon?trusted=true")
    arm = ref.variant()
    assert isinstance(arm, StoreReferenceSpecified)
    assert (arm.scheme(), arm.authority()) == ("unix", "")
    assert ref.params() == {"trusted": "true"}


def test_a_uri_names_its_scheme_authority_and_params() -> None:
    ref = parse_store_reference("ssh-ng://user@host?compress=true")
    arm = ref.variant()
    assert isinstance(arm, StoreReferenceSpecified)
    assert (arm.scheme(), arm.authority()) == ("ssh-ng", "user@host")
    assert ref.params() == {"compress": "true"}
    assert ref.render() == "ssh-ng://user@host?compress=true"
    assert ref.render(with_params=False) == "ssh-ng://user@host"


def test_a_bare_path_is_a_local_uri() -> None:
    arm = parse_store_reference("/tmp/x").variant()
    assert isinstance(arm, StoreReferenceSpecified)
    assert (arm.scheme(), arm.authority()) == ("local", "/tmp/x")


def test_what_libstore_cannot_read_raises() -> None:
    with pytest.raises(UsageError, match="Cannot parse Nix store"):
        parse_store_reference("not a uri")
