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
from cythonix_bindings.errors import BadStorePath, NixError, UsageError

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

    Fresh, so the list is empty - and the empty case is worth its own
    test, because a repeated field has no presence. Nothing on the
    wire tells "no paths" from "field never set", so both sides have
    to mean the same thing by it."""
    store = Store(str(tmp_path))
    assert store.query_all_valid_paths() == []


@pytest.fixture
def chroot(tmp_path: pathlib.Path) -> Store:
    """A real LocalStore rooted somewhere writable.

    It needs no daemon and no /nix/var, so it runs in a build sandbox
    - which is what moved store-writing tests back INSIDE the build
    rather than out of it (tasks/037)."""
    return Store(str(tmp_path))


def test_a_store_takes_bytes_and_names_the_result(chroot: Store) -> None:
    """The contents decide the name, which is the whole idea.

    Same bytes and same method give the same path every time, and
    different bytes give a different one - that is content addressing,
    and it is why `data` is bytes rather than str: the hash is of
    exactly these bytes, and an encoding guess would change the name
    of the thing."""
    path = chroot.add_to_store("greeting", b"hello world\n", "text", "sha256")
    assert path.name() == "greeting"
    assert chroot.is_valid_path(path)

    again = chroot.add_to_store("greeting", b"hello world\n", "text", "sha256")
    assert again.to_string() == path.to_string()

    other = chroot.add_to_store("greeting", b"goodbye\n", "text", "sha256")
    assert other.to_string() != path.to_string()

    # ...and the method is part of the name too, not a formality.
    flat = chroot.add_to_store("greeting", b"hello world\n", "flat", "sha256")
    assert flat.to_string() != path.to_string()


def test_a_store_hands_back_every_path_it_holds(chroot: Store) -> None:
    """query_all_valid_paths, on a store that HOLDS something.

    Each element arrives as a heap pointer the binding owns, because
    nix::StorePath has no default constructor and Cython cannot hold
    one in a loop temporary. The loop gives each pointer to a wrapper
    and blanks the slot, so the finally clause frees only what never
    got one. Skip the blanking and reading these names is a
    use-after-free.

    Until a store could be WRITTEN to, this needed the live system
    (tasks/037). It does not any more."""
    names = ["alpha", "beta", "gamma"]
    for name in names:
        chroot.add_to_store(name, name.encode(), "text", "sha256")

    held = chroot.query_all_valid_paths()
    assert sorted(p.name() for p in held) == names

    # Read every one after dropping the list that owned them: a double
    # free takes the process with it, so getting here IS the assertion.
    base = sorted(p.to_string() for p in held)
    del held
    gc.collect()
    assert sorted(p.to_string() for p in chroot.query_all_valid_paths()) == base


def test_the_store_names_the_vocabulary_it_accepts(chroot: Store) -> None:
    """method and hash_algo are Nix's words, parsed by Nix.

    So an invented one fails with libstore's own message, listing what
    it would have taken - which no table in this repo has to hold, and
    so cannot get out of date."""
    with pytest.raises(UsageError, match="expect `flat`, `nar`, or `git`"):
        chroot.add_to_store("x", b"y", "nonsense", "sha256")
    with pytest.raises(UsageError, match="unknown hash algorithm"):
        chroot.add_to_store("x", b"y", "text", "md6")


def test_bytes_are_bytes(chroot: Store) -> None:
    """A str is refused rather than encoded on the caller's behalf.

    Guessing utf-8 would put a different byte string in the store than
    the caller passed, under a name that is the hash of the guess."""
    with pytest.raises(TypeError):
        chroot.add_to_store("x", "not bytes", "text", "sha256")  # type: ignore[arg-type]


@pytest.mark.live
def test_a_populated_store_hands_over_every_path(ambient_store: Store) -> None:
    """The ownership loop, against the machine's own store.

    A chroot store covers that loop in the sandbox now. What is left
    to this one is the store a sandbox still has none of: `auto` is
    the daemon, a different implementation on both sides of a socket,
    holding tens of thousands of paths rather than three."""
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
