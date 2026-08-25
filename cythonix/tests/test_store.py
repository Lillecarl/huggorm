"""
The real nix::Store (tasks/015).

Abstract in C++ and chosen by a URI, so the binding is built through
openStore rather than a constructor. "dummy://" is in-memory and needs
nothing on disk, which is what makes it testable in a build sandbox.
"""

import gc
import pathlib

import pytest

from cythonix_bindings import Store, StorePath
from cythonix_bindings.errors import BadStorePath, NixError

HELLO = "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"


@pytest.fixture
def store() -> Store:
    return Store("dummy://")


def test_a_store_opens_from_a_uri(store: Store) -> None:
    assert store.get_uri() == "dummy://"


def test_an_unknown_scheme_is_refused() -> None:
    """openStore decides the implementation, so a URI it cannot read is
    the first thing a caller gets wrong."""
    with pytest.raises(NixError, match="don't know how to open"):
        Store("bogus://nowhere")


def test_a_store_prints_and_parses_paths(store: Store) -> None:
    printed = store.print_store_path(StorePath(HELLO))
    assert printed == f"/nix/store/{HELLO}"
    assert store.parse_store_path(printed).to_string() == HELLO


def test_parsing_checks_the_store_directory(store: Store) -> None:
    """A different question from whether the NAME is well formed, which
    is why it lives on the store rather than on StorePath: only the
    store knows its own directory."""
    with pytest.raises(BadStorePath, match="is not in the Nix store"):
        store.parse_store_path("/somewhere/else/x")


def test_an_empty_store_holds_nothing(store: Store) -> None:
    assert store.is_valid_path(StorePath(HELLO)) is False


def test_a_store_need_not_answer_for_all_its_paths(store: Store) -> None:
    """nix::Store's own queryAllValidPaths raises rather than returning
    nothing, and only the local and remote stores override it. That is
    honest: a substituter has no such list to give, and an empty answer
    would be a lie rather than a limitation."""
    with pytest.raises(NixError, match="not supported by store"):
        store.query_all_valid_paths()


def test_a_local_store_answers_with_a_list(tmp_path: pathlib.Path) -> None:
    """A chroot store: a real LocalStore rooted somewhere writable,
    which is what makes a store that IMPLEMENTS the query runnable in a
    build sandbox at all.

    It is empty, and it stays empty: nothing in these bindings writes
    to a store yet. So this covers the answer being a list rather than
    an error, and the empty case that a repeated field cannot tell from
    an unset one. The loop that takes ownership of each element needs a
    store that HOLDS something, which is the live test below."""
    store = Store(str(tmp_path))
    assert store.query_all_valid_paths() == []


@pytest.mark.live
def test_a_populated_store_hands_over_every_path(ambient_store: Store) -> None:
    """The ownership loop, on a store with real paths in it.

    Each element arrives as a heap pointer the binding owns, because
    nix::StorePath has no default constructor and Cython cannot hold
    one in a loop temporary. So the loop hands each pointer to a
    wrapper and blanks the slot; a mistake there is a double free or a
    dangling read, and neither shows up on the empty list a sandbox can
    build.

    Which is why this one is `live`. It needs a store that holds
    something, and a build has none (tasks/037)."""
    paths = ambient_store.query_all_valid_paths()
    assert paths, "the machine's own store holds no paths at all"
    assert all(isinstance(p, StorePath) for p in paths)

    # Every element is a live object, not a pointer into freed memory:
    # read each one, and round-trip a sample through the store that
    # produced it. Bounded, because a real store holds tens of
    # thousands and reading them all proves nothing more.
    names = [p.to_string() for p in paths]
    assert all(names), "a store path with an empty base name"
    for path in paths[:50]:
        assert ambient_store.is_valid_path(path)
        printed = ambient_store.print_store_path(path)
        assert ambient_store.parse_store_path(printed).to_string() \
            == path.to_string()

    # Ask twice with everything from the first answer dropped in
    # between. A double free takes the process with it, so surviving
    # this IS the assertion; the count is the cheap part.
    del paths
    gc.collect()
    assert [p.to_string() for p in ambient_store.query_all_valid_paths()] \
        == names


def test_the_binding_initialises_libstore() -> None:
    """libstore ABORTS rather than raising when it has not been
    initialised - "The program must call nix::initNix() before calling
    any libstore library functions" - so a caller can never be the one
    to discover it. Importing the module is what runs it, and this test
    passing at all is the evidence."""
    import cythonix_bindings.store

    assert cythonix_bindings.store.Store is Store
