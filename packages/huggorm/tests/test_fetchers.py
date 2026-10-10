"""nix::fetchers::Input: what a fetcher fetches, before it fetches."""

import pathlib
import subprocess

import pytest
from nixversion import NOT_SUPPORTED_BY_SCHEME

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


def test_equal_inputs_hash_equal_whatever_the_attribute_order() -> None:
    from huggorm_bindings import input_from_attrs

    one = input_from_attrs({"type": "github", "owner": "NixOS", "repo": "nixpkgs"})
    two = input_from_attrs({"repo": "nixpkgs", "owner": "NixOS", "type": "github"})
    assert one == two
    assert hash(one) == hash(two)
    assert len({one, two}) == 1


def test_a_scheme_refuses_an_attribute_it_does_not_take() -> None:
    from huggorm_bindings import input_from_attrs
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError, match=f"attribute 'owner' {NOT_SUPPORTED_BY_SCHEME} 'path'"):
        input_from_attrs({"type": "path", "path": "/src", "owner": "x"})


def _dirty(tmp_path: pathlib.Path) -> pathlib.Path:
    """A git work tree with one commit and one uncommitted change."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-c", "user.name=huggorm",
                        "-c", "user.email=huggorm@invalid", *args],
                       cwd=repo, check=True, capture_output=True)

    (repo / "a").write_text("committed")
    git("init", "--quiet")
    git("add", "a")
    git("commit", "--quiet", "--message", "one")
    (repo / "a").write_text("changed")
    return repo


def test_a_dirty_work_tree_is_named_by_its_changes(
        tmp_path: pathlib.Path) -> None:
    """Uncommitted changes are hashed into the fingerprint, which is
    the case that makes `fingerprint` block: Nix reads each changed
    file. Change the file and the answer changes."""
    from huggorm_bindings import Store, input_from_attrs

    repo = _dirty(tmp_path)
    store = Store("dummy://")
    source = input_from_attrs({"type": "git", "url": f"file://{repo}"})
    first = source.fingerprint(store)
    assert first is not None and ";d=" in first, first
    assert input_from_attrs(source.to_attrs()).fingerprint(store) == first
    (repo / "a").write_text("changed again")
    again = input_from_attrs({"type": "git", "url": f"file://{repo}"})
    assert again.fingerprint(store) not in (None, first)


async def test_a_blocking_method_on_a_value_is_awaitable(
        tmp_path: pathlib.Path) -> None:
    """`Input.fingerprint` blocks, and `Input` is a value with no
    wrapper, so its async form is a free coroutine. It takes the
    store as either form, as every free coroutine does (huggorm#25)."""
    from huggorm_bindings import Store, input_from_attrs
    from huggorm_generated import AsyncStore, input_fingerprint

    repo = _dirty(tmp_path)
    source = input_from_attrs({"type": "git", "url": f"file://{repo}"})
    expected = source.fingerprint(Store("dummy://"))
    assert expected is not None
    assert await input_fingerprint(source, Store("dummy://")) == expected
    store = AsyncStore("dummy://")
    try:
        assert await input_fingerprint(source, store) == expected
    finally:
        await store.aclose()
