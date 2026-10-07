"""A Nix store implemented in Python, layered over another (huggorm#149).

Nix calls these stores; the Python object answers what it chooses and
leaves the rest to its underlying store. In-process only: a factory is
a Python callable, and a store registration is process-wide.
"""

import itertools
from typing import Any

import pytest

from huggorm_bindings import Store, register_store_implementation
from huggorm_bindings.errors import InvalidPath, Unsupported

_names = itertools.count()
MISSING = "/nix/store/00000000000000000000000000000000-absent"


def register(factory: Any) -> str:
    """A fresh scheme: a registration lasts the whole process."""
    name = f"py-test-{next(_names)}"
    register_store_implementation(name, [name], factory)
    return name


class Layer:
    """A Python store with no hooks: everything falls through."""

    def __init__(self, underlying: Any = None) -> None:
        self.underlying = underlying


def layered(cls: type, underlying: Any = None) -> Any:
    return Store(f"{register(lambda *_: cls(underlying))}://")


@pytest.fixture
def under() -> Any:
    return Store("dummy://?read-only=false")


def test_a_scheme_opens_through_the_factory() -> None:
    seen: list[tuple[str, str, dict[str, str]]] = []

    def factory(scheme: str, authority: str, params: dict[str, str]) -> Any:
        seen.append((scheme, authority, params))
        return Layer()

    name = register(factory)
    Store(f"{name}://there?flavour=sweet&path-info-cache-size=0")
    assert seen == [(name, "there", {"flavour": "sweet"})], (
        "the factory gets only the parameters Nix does not take")


def test_a_name_registers_once() -> None:
    name = register(lambda *_: Layer())
    with pytest.raises(Exception, match="already registered"):
        register_store_implementation(name, ["py-test-other"],
                                      lambda *_: Layer())


def test_with_nothing_below_an_unanswered_operation_is_unsupported() -> None:
    store = layered(Layer)
    with pytest.raises(Unsupported, match="queryAllValidPaths"):
        store.query_all_valid_paths()
    with pytest.raises(Unsupported, match="addToStore"):
        store.add_to_store("x", b"x")


def test_a_hook_answers(under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Valid(Layer):
        def is_valid_path(self, path: Any) -> bool:
            return bool(path == held)

    store = layered(Valid)
    assert store.is_valid_path(held)
    assert not store.is_valid_path(store.parse_store_path(MISSING))


def test_what_python_does_not_answer_runs_below(under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Declines(Layer):
        def is_valid_path(self, path: Any) -> Any:
            return NotImplemented

    store = layered(Declines, under)
    assert store.is_valid_path(held), "NotImplemented falls through"


def test_an_undefined_hook_runs_below(under: Any) -> None:
    held = under.add_to_store("held", b"held")
    store = layered(Layer, under)
    assert store.query_path_info(held).nar_hash() == (
        under.query_path_info(held).nar_hash()), "an undefined hook falls through"
    added = store.add_to_store("added", b"added")
    assert under.is_valid_path(added), "a write lands below"
    with pytest.raises(Unsupported, match="by store 'dummy://'"):
        store.query_all_valid_paths()  # the dummy store's own refusal


def test_path_info_comes_from_python(under: Any) -> None:
    held = under.add_to_store("held", b"held")
    info = under.query_path_info(held)

    class Info(Layer):
        def query_path_info(self, path: Any) -> Any:
            return info if path == held else None

    store = layered(Info)
    assert store.query_path_info(held).nar_hash() == info.nar_hash()
    with pytest.raises(InvalidPath):
        store.query_path_info(store.parse_store_path(MISSING))


def test_a_raising_hook_is_an_error_not_a_fall_through(under: Any) -> None:
    held = under.add_to_store("held", b"held")

    class Raises(Layer):
        def is_valid_path(self, path: Any) -> bool:
            raise ValueError("from the hook")

        def query_path_info(self, path: Any) -> Any:
            raise ValueError("from the info hook")

    store = layered(Raises, under)
    with pytest.raises(ValueError, match="from the hook"):
        store.is_valid_path(held)
    with pytest.raises(ValueError, match="from the info hook"):
        store.query_path_info(held)
