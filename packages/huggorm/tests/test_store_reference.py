"""A store URI as libstore reads it (`store-reference.cc`).

Each case is one branch of `StoreReference::parse`, and the arm it
lands in is the fact under test: `daemon` and `daemon?x=y` are
different arms upstream, and a caller must be told which it got.
"""

from typing import Any

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


async def test_the_async_form_adopts_a_wrapper_class() -> None:
    """A free function that returns a WRAPPER class hands back its
    async form, through `_adopt`, as a returned type's does."""
    from huggorm_generated import AsyncStoreReference
    from huggorm_generated import parse_store_reference as aparse

    ref = await aparse("ssh-ng://user@host?compress=true")
    assert isinstance(ref, AsyncStoreReference)
    assert await ref.render(with_params=False) == "ssh-ng://user@host"
    assert await ref.params() == {"compress": "true"}


async def test_a_remote_parse_answers_a_handle(client: Any) -> None:
    """Over the wire the same call answers a handle the server leased,
    and the handle's methods reach the adopted object."""
    ref = await client.call_function("parse_store_reference",
                                     "daemon?trusted=true")
    assert ref.handle_id
    assert await ref.params() == {"trusted": "true"}
    assert await ref.render() == "unix://?trusted=true"


def test_the_store_registry_names_nix_s_own_types() -> None:
    """The document `nix __dump-cli` prints under `stores`."""
    import json

    from huggorm_bindings import store_types_json

    types = json.loads(store_types_json())
    local = types["Local Store"]
    assert "local" in local["uri-schemes"]
    assert "root" in local["settings"]
    assert local["experimentalFeature"] is None
    assert types["Experimental Local Overlay Store"]["experimentalFeature"] \
        == "local-overlay-store"
