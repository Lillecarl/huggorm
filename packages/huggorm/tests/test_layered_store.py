"""A Nix store implemented in Python, layered over another (huggorm#149).

A subclass of `LayeredStore` overrides the hooks it answers; Nix calls
them through the emitted trampoline, and everything else runs on the
underlying store. In-process only: a registration is process-wide and
keeps a Python callable.
"""

import itertools
from typing import Any

import pytest

from huggorm_bindings import (
    LayeredStore,
    LayeredStoreConfig,
    Store,
    register_store_implementation,
)
from huggorm_bindings.errors import InvalidPath, Unsupported

_names = itertools.count()
MISSING = "/nix/store/00000000000000000000000000000000-absent"


def register(factory: Any) -> str:
    """A fresh scheme: a registration lasts the whole process."""
    name = f"layered-test-{next(_names)}"
    register_store_implementation(name, [name], factory)
    return name


def opened(cls: type, underlying: Any = None) -> Any:
    """`cls` over `underlying`, opened through its scheme."""
    def factory(config: LayeredStoreConfig) -> Any:
        return cls(config, underlying)
    return Store(f"{register(factory)}://")


@pytest.fixture
def under() -> Any:
    return Store("dummy://?read-only=false")


def test_a_subclass_opens_through_its_scheme() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    class Mine(LayeredStore):
        def __init__(self, config: LayeredStoreConfig) -> None:
            seen.append((config.scheme(), config.authority(),
                         config.params()))
            super().__init__(config)

    name = register(Mine)
    Store(f"{name}://there?flavour=sweet&path-info-cache-size=0")
    assert seen == [(name, "there", {"flavour": "sweet"})], (
        "the subclass gets only the parameters Nix does not take")


def test_a_factory_must_answer_a_layered_store() -> None:
    name = register(lambda config: object())
    with pytest.raises(Exception, match="not a LayeredStore"):
        Store(f"{name}://")


def test_a_name_registers_once() -> None:
    name = register(LayeredStore)
    with pytest.raises(Exception, match="already registered"):
        register_store_implementation(name, ["layered-test-other"],
                                      LayeredStore)


def test_with_nothing_below_an_unanswered_operation_is_unsupported() -> None:
    store = opened(LayeredStore)
    with pytest.raises(Unsupported, match="queryAllValidPaths"):
        store.query_all_valid_paths()
    with pytest.raises(Unsupported, match="addToStore"):
        store.add_to_store("x", b"x")


def test_an_override_answers(under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Valid(LayeredStore):
        def is_valid_path(self, path: Any) -> bool:
            return bool(path == held)

    store = opened(Valid)
    assert store.is_valid_path(held)
    assert not store.is_valid_path(store.parse_store_path(MISSING))


def test_super_runs_below(under: Any) -> None:
    """`super()` from an override reaches the C++ fall-through, not the
    override again."""
    held = under.add_to_store("held", b"held")
    calls: list[Any] = []

    class Counts(LayeredStore):
        def is_valid_path(self, path: Any) -> bool:
            calls.append(path)
            return bool(super().is_valid_path(path))

    store = opened(Counts, under)
    assert store.is_valid_path(held)
    assert calls == [held]


def test_an_operation_with_no_override_runs_below(under: Any) -> None:
    held = under.add_to_store("held", b"held")
    store = opened(LayeredStore, under)
    assert store.query_path_info(held).nar_hash() == (
        under.query_path_info(held).nar_hash())
    added = store.add_to_store("added", b"added")
    assert under.is_valid_path(added), "a write lands below"
    with pytest.raises(Unsupported, match="by store 'dummy://'"):
        store.query_all_valid_paths()  # the dummy store's own refusal


def test_nix_reaches_the_override_and_reads_its_error(under: Any) -> None:
    """Only `query_path_info` is overridden. `is_valid_path` runs Nix's
    own default, which asks path info and catches `InvalidPath`: so the
    override's Python InvalidPath must reach Nix as the Nix one."""
    held = under.add_to_store("held", b"held")
    info = under.query_path_info(held)
    asked: list[Any] = []

    class Info(LayeredStore):
        def query_path_info(self, path: Any) -> Any:
            asked.append(path)
            if path != held:
                raise InvalidPath(f"{path} is not here")
            return info

    store = opened(Info)
    missing = store.parse_store_path(MISSING)
    assert store.is_valid_path(held)
    assert not store.is_valid_path(missing)
    assert asked == [held, missing], "Nix asked the override"


def test_list_hooks_answer(under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Lists(LayeredStore):
        def query_all_valid_paths(self) -> list[Any]:
            return [held]

        def query_referrers(self, path: Any) -> list[Any]:
            return [held]

    store = opened(Lists)
    assert store.query_all_valid_paths() == [held]
    assert store.query_referrers(held) == [held]


def test_a_raising_override_is_an_error_not_a_fall_through(
        under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Raises(LayeredStore):
        def is_valid_path(self, path: Any) -> bool:
            raise ValueError("from the hook")

        def query_path_info(self, path: Any) -> Any:
            raise ValueError("from the info hook")

    store = opened(Raises, under)
    with pytest.raises(ValueError, match="from the hook"):
        store.is_valid_path(held)
    with pytest.raises(ValueError, match="from the info hook"):
        store.query_path_info(held)
