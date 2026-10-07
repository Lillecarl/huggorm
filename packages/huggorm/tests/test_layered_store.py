"""A Nix store implemented in Python, layered over another (huggorm#149).

A subclass of `LayeredStore` overrides the hooks it answers; Nix calls
them through the emitted trampoline, and everything else runs on the
underlying store. In-process only: a registration is process-wide and
keeps a Python callable.
"""

import itertools
from typing import Any

import pytest
from nixversion import drv_output

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


def test_nix_reaches_every_hook(under: Any) -> None:
    """`Store.<method>(store, ...)` is `Store`'s own binding, so each
    call enters Nix and reaches the override through the C++ virtual.
    Calling the method on the instance would find the override in
    Python and prove nothing."""
    held = under.add_to_store("held", b"held")
    missing = under.parse_store_path(MISSING)
    called: list[str] = []

    class Every(LayeredStore):
        def query_valid_derivers(self, path: Any) -> list[Any]:
            called.append("query_valid_derivers")
            return [held]

        def query_valid_paths(self, paths: list[Any]) -> list[Any]:
            called.append("query_valid_paths")
            return [p for p in paths if p == held]

        def compute_fs_closure(
                self, paths: list[Any], flip_direction: bool = False,
                include_outputs: bool = False,
                include_derivers: bool = False) -> list[Any]:
            flags = (flip_direction, include_outputs, include_derivers)
            called.append(f"compute_fs_closure{flags}")
            return [held]

        def query_substitutable_paths(self, paths: list[Any]) -> list[Any]:
            called.append("query_substitutable_paths")
            return []

        def query_missing(self, targets: list[Any]) -> Any:
            called.append("query_missing")
            return super().query_missing(targets)

        def query_realisation(self, id: Any) -> Any:
            called.append("query_realisation")
            return None

        def read_derivation(self, path: Any) -> Any:
            called.append("read_derivation")
            raise InvalidPath(f"{path} is no derivation here")

        def optimise_store(self) -> None:
            called.append("optimise_store")

        def verify_store(self, check_contents: bool,
                         repair: bool = False) -> bool:
            called.append(f"verify_store{check_contents, repair}")
            return True

    store = opened(Every)
    assert Store.query_valid_derivers(store, held) == [held]
    assert Store.query_valid_paths(store, [held, missing]) == [held]
    assert Store.compute_fs_closure(store, [missing], True) == [held]
    assert Store.query_substitutable_paths(store, [held]) == []
    assert Store.query_missing(store, []).will_build() == []
    assert Store.query_realisation(store, drv_output(1, "out")) is None
    with pytest.raises(InvalidPath, match="no derivation here"):
        Store.read_derivation(store, held)
    Store.optimise_store(store)
    assert Store.verify_store(store, False, True)
    assert called == [
        "query_valid_derivers", "query_valid_paths",
        "compute_fs_closure(True, False, False)",
        "query_substitutable_paths", "query_missing", "query_realisation",
        "read_derivation", "optimise_store", "verify_store(False, True)"]


def test_a_closure_from_path_info_alone(under: Any) -> None:
    """With nothing below, `compute_fs_closure` and `query_valid_paths`
    run Nix's own defaults, which ask `query_path_info`."""
    leaf = under.add_to_store("leaf", b"leaf")
    top = under.add_to_store("top", b"top", references=[leaf])

    class Info(LayeredStore):
        def query_path_info(self, path: Any) -> Any:
            if path not in (leaf, top):
                raise InvalidPath(f"{path} is not here")
            return under.query_path_info(path)

    store = opened(Info)
    missing = store.parse_store_path(MISSING)
    assert store.compute_fs_closure([top]) == sorted([leaf, top])
    assert store.query_valid_paths([top, missing]) == [top]


def test_a_copy_asks_the_destination_which_paths_it_holds(
        under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Claims(LayeredStore):
        def query_valid_paths(self, paths: list[Any]) -> list[Any]:
            return list(paths)

    below = Store("dummy://?read-only=false")
    under.copy_closure(opened(Claims, below), [held],
                       check_sigs=False)
    assert not below.is_valid_path(held), "Nix skips what the hook claims"
    under.copy_closure(opened(LayeredStore, below), [held],
                       check_sigs=False)
    assert below.is_valid_path(held), "and copies without the claim"


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
