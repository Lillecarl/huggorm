"""nix::fetchers::Input: what a fetcher fetches, before it fetches."""

import pathlib

import pytest

# Every fetcher scheme but `path` sits behind the `flakes` feature.
pytestmark = pytest.mark.usefixtures("flakes")


def test_a_url_parses_into_its_attributes() -> None:
    from huggorm_bindings import input_from_url

    parsed = input_from_url("github:NixOS/nixpkgs")
    attrs = parsed.to_attrs()
    assert (attrs["type"], attrs["owner"], attrs["repo"]) == (
        "github", "NixOS", "nixpkgs")
    assert parsed.to_string() == "github:NixOS/nixpkgs"


def test_attributes_and_a_url_name_one_input() -> None:
    from huggorm_bindings import input_from_attrs, input_from_url

    parsed = input_from_url("github:NixOS/nixpkgs")
    assert input_from_attrs(parsed.to_attrs()) == parsed


def test_a_scheme_refuses_an_attribute_it_does_not_take() -> None:
    from huggorm_bindings import input_from_attrs
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError, match="attribute 'owner' not supported by scheme 'path'"):
        input_from_attrs({"type": "path", "path": "/src", "owner": "x"})


def test_a_path_input_names_its_contents(tmp_path: pathlib.Path) -> None:
    """A path input can say what its tree is without a store copy;
    the same tree gives the same fingerprint."""
    from huggorm_bindings import Store, input_from_attrs

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a").write_text("x")
    store = Store("dummy://")
    source = input_from_attrs({"type": "path", "path": str(tmp_path / "src")})
    first = source.fingerprint(store)
    assert first == source.fingerprint(store)
