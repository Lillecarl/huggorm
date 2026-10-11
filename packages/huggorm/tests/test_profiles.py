"""Profiles: generation links, as `nix ... --profile` and `nix-env` make
them, against a chroot store."""

import os
import pathlib

import pytest

from huggorm_bindings import ContentAddressMethod as CA
from huggorm_bindings import HashAlgorithm, Store
from huggorm_bindings.profiles import (
    current_generation,
    delete_old_generations,
    find_generations,
    set_profile,
    switch_generation,
)


@pytest.fixture
def chroot(tmp_path: pathlib.Path) -> Store:
    return Store(str(tmp_path / "store"))


def test_a_profile_points_at_a_new_generation_only_on_a_change(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    one = chroot.add_to_store("one", b"1", CA.NAR, HashAlgorithm.SHA256)
    two = chroot.add_to_store("two", b"2", CA.NAR, HashAlgorithm.SHA256)
    profile = tmp_path / "profiles" / "salt"
    profile.parent.mkdir()

    first = set_profile(chroot, profile, one)
    assert first == profile.with_name("salt-1-link")
    assert os.readlink(profile) == "salt-1-link"
    assert os.readlink(first) == chroot.print_store_path(one)

    assert set_profile(chroot, profile, one) == first
    assert [g.number() for g in find_generations(profile)] == [1]

    second = set_profile(chroot, profile, two)
    assert second == profile.with_name("salt-2-link")
    assert current_generation(profile) == 2
    assert [g.number() for g in find_generations(profile)] == [1, 2]
    assert [g.path() for g in find_generations(profile)] == [first, second]


def test_deleting_old_generations_keeps_the_current_one(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    profile = tmp_path / "p"
    for name in ("a", "b", "c"):
        path = chroot.add_to_store(name, name.encode(), CA.NAR, HashAlgorithm.SHA256)
        set_profile(chroot, profile, path)
    delete_old_generations(profile)
    assert [g.number() for g in find_generations(profile)] == [3]
    assert current_generation(profile) == 3


def test_a_rollback_points_at_the_previous_generation(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    from huggorm_bindings.errors import NixError

    profile = tmp_path / "p"
    for name in ("a", "b", "c"):
        path = chroot.add_to_store(name, name.encode(), CA.NAR, HashAlgorithm.SHA256)
        set_profile(chroot, profile, path)

    switch_generation(profile, dry_run=True)
    assert current_generation(profile) == 3
    switch_generation(profile)
    assert current_generation(profile) == 2
    switch_generation(profile, 3)
    assert current_generation(profile) == 3
    switch_generation(profile, 1)
    with pytest.raises(NixError, match="no profile version older"):
        switch_generation(profile)
    with pytest.raises(NixError, match="does not exist"):
        switch_generation(profile, 9)
    assert current_generation(profile) == 1


def test_a_profile_that_does_not_exist_has_no_generations(tmp_path: pathlib.Path) -> None:
    assert find_generations(tmp_path / "none") == []
    assert current_generation(tmp_path / "none") is None


def test_a_profile_needs_a_local_store(tmp_path: pathlib.Path) -> None:
    from huggorm_bindings.errors import NixError

    store = Store("dummy://?read-only=false")
    path = store.add_to_store("x", b"x", CA.NAR, HashAlgorithm.SHA256)
    with pytest.raises(NixError, match="local store"):
        set_profile(store, tmp_path / "p", path)
