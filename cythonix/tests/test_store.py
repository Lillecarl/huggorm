"""
The real nix::Store (tasks/015).

Abstract in C++ and chosen by a URI, so the binding is built through
openStore rather than a constructor. "dummy://" is in-memory and needs
nothing on disk, which is what makes it testable in a build sandbox.
"""

import gc
import pathlib
import sys

import pytest

from cythonix_bindings import ContentAddressMethod as CA
from cythonix_bindings import HashAlgorithm, PathInfo, Store, StorePath
from cythonix_bindings.errors import (
    BadStorePath,
    InvalidPath,
    NixError,
    Unsupported,
    UsageError,
)

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
    would be a lie rather than a limitation.

    Unsupported, not NixError. The distinction is worth catching by
    type: a caller can fall back to another store when this one cannot
    answer, and cannot fall back from a call that went wrong."""
    with pytest.raises(Unsupported, match="not supported by store"):
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
    path = chroot.add_to_store(
        "greeting", b"hello world\n", CA.TEXT, HashAlgorithm.SHA256)
    assert path.name() == "greeting"
    assert chroot.is_valid_path(path)

    again = chroot.add_to_store(
        "greeting", b"hello world\n", CA.TEXT, HashAlgorithm.SHA256)
    assert again.to_string() == path.to_string()

    other = chroot.add_to_store(
        "greeting", b"goodbye\n", CA.TEXT, HashAlgorithm.SHA256)
    assert other.to_string() != path.to_string()

    # ...and the method is part of the name too, not a formality.
    flat = chroot.add_to_store(
        "greeting", b"hello world\n", CA.FLAT, HashAlgorithm.SHA256)
    assert flat.to_string() != path.to_string()


def test_the_defaults_are_libstores_own(chroot: Store) -> None:
    """A short call is the call Nix itself would have made.

    addToStoreFromDump declares `hashMethod = NixArchive` and
    `hashAlgo = SHA256`, and this binding repeats them rather than
    picking. So the two-argument call is not a shorthand for something
    invented here - it lands on the same path as spelling both out.

    The second half is what makes that worth asserting: a DIFFERENT
    method gives a different path. Without it, "the default matches
    NAR" would also pass if the argument were ignored."""
    short = chroot.add_to_store("greeting", b"hello world\n")
    spelled = chroot.add_to_store(
        "greeting", b"hello world\n", CA.NAR, HashAlgorithm.SHA256)
    assert short.to_string() == spelled.to_string()

    flat = chroot.add_to_store(
        "greeting", b"hello world\n", CA.FLAT, HashAlgorithm.SHA256)
    assert flat.to_string() != short.to_string()


@pytest.fixture
def source(tmp_path: pathlib.Path) -> pathlib.Path:
    """A small tree to add. Beside the store root, not inside it: a
    chroot store's real directory is <root>/nix/store."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello\n")
    (src / "sub" / "b.txt").write_text("world\n")
    return src


def test_a_store_takes_a_path_and_reads_it(
        chroot: Store, source: pathlib.Path) -> None:
    """add_path_to_store, on a DIRECTORY.

    The reason this overload exists. A directory has no contents to
    hand over as bytes, so `add_to_store` cannot take one at all - and
    `nar` is the only method that can describe a tree, which is why it
    is the default.

    A chroot store keeps `/nix/store` as its store directory and puts
    the files under <root>/nix/store, so the printed path is virtual
    and the real one is the root joined onto it."""
    path = chroot.add_path_to_store("tree", str(source))
    assert path.name() == "tree"
    assert chroot.is_valid_path(path)

    real = chroot.real_path(path)
    assert sorted(p.name for p in real.iterdir()) == ["a.txt", "sub"]
    assert (real / "sub" / "b.txt").read_text() == "world\n"


def test_the_two_overloads_agree_about_one_file(
        chroot: Store, source: pathlib.Path) -> None:
    """A regular file added either way lands on the same path.

    Which is the sharpest thing that can be said about them: the name
    is the hash of the contents, so agreeing means both really saw the
    same bytes under the same method. Checked for both, because the
    method changes the hash and could hide a disagreement."""
    for method in (CA.FLAT, CA.NAR):
        by_path = chroot.add_path_to_store(
            "one", str(source / "a.txt"), method, HashAlgorithm.SHA256)
        by_bytes = chroot.add_to_store(
            "one", b"hello\n", method, HashAlgorithm.SHA256)
        assert by_path.to_string() == by_bytes.to_string(), method


def test_a_store_says_where_its_files_really_are(
        chroot: Store, source: pathlib.Path,
        tmp_path: pathlib.Path) -> None:
    """real_path, and why it is not print_store_path.

    A chroot store keeps /nix/store as its store DIRECTORY and puts
    the files under <root>/nix/store. So the printed path is the same
    string a full-system store would print and does not exist here,
    while the real one does. Asserting both is the point: one of them
    is a location and the other is a name."""
    path = chroot.add_path_to_store("tree", str(source))

    printed = chroot.print_store_path(path)
    assert printed == f"/nix/store/{path.to_string()}"

    real = chroot.real_path(path)
    assert isinstance(real, pathlib.Path)
    assert real == tmp_path / "nix/store" / path.to_string()
    assert real.is_dir() and (real / "a.txt").read_text() == "hello\n"
    assert not pathlib.Path(printed).exists()


def test_a_store_with_no_filesystem_has_no_real_path(store: Store) -> None:
    """Unsupported, and it comes from where the answer would be.

    libstore puts toRealPath on LocalFSStore rather than on Store,
    because a binary cache or an ssh-ng store has no directory here at
    all. The dummy store is one of those, so the shim asks whether the
    store IS a LocalFSStore and refuses in libstore's own words when
    it is not."""
    with pytest.raises(Unsupported, match="not supported by store"):
        store.real_path(StorePath(HELLO))


def test_a_store_answers_for_a_path_it_holds(
        chroot: Store, source: pathlib.Path) -> None:
    """query_path_info, on a path this store just took.

    Every field comes from the store's own database rather than from
    the call that added it, so this is what the store BELIEVES, not an
    echo. The NAR size is the assertion that proves that: nothing here
    told it a size, and it is not the size of the bytes on disk."""
    path = chroot.add_path_to_store("tree", str(source))
    info = chroot.query_path_info(path)

    assert info.path().to_string() == path.to_string()
    assert info.nar_hash().startswith("sha256:")
    assert info.nar_size() > 0

    # Added, not built, so nothing derived it - and None is the answer
    # rather than a gap.
    assert info.deriver() is None
    assert info.ultimate() is False

    # Zero, and that is Nix's answer rather than a missing field: an
    # added path gets no registration time stamped on it. The live
    # test below is where a real one shows up.
    assert info.registration_time() == 0

    # Both empty, and both for a reason rather than by omission. This
    # add pins references to empty - Nix does not scan an added path
    # for them, it is told - and nothing signs a path a store added
    # itself.
    assert info.references() == []
    assert info.sigs() == []


def test_a_store_records_the_references_it_is_told(
        chroot: Store, source: pathlib.Path) -> None:
    """The other direction of the same field.

    Nix does not scan an added path for references - it is told them -
    so this is the only way a store ever learns one for a path that
    was added rather than built. Reading it back through
    query_path_info is what proves the two halves name the same
    thing."""
    target = chroot.add_path_to_store("tree", str(source))
    holder = chroot.add_to_store("holder", b"points at a tree\n",
                                 references=[target])

    info = chroot.query_path_info(holder)
    assert [r.to_string() for r in info.references()] == [target.to_string()]

    # None and [] are the same answer, and both are the default. A
    # repeated field has no presence, so there is nothing else absence
    # could mean.
    nothing: list[list[StorePath] | None] = [None, []]
    for empty in nothing:
        alone = chroot.add_to_store("alone", b"points at nothing\n",
                                    references=empty)
        assert chroot.query_path_info(alone).references() == []


def test_a_reference_must_be_a_store_path(chroot: Store) -> None:
    """The list is typed, so a string is refused before libstore sees
    it. A base name is not a StorePath even when it reads like one:
    only the class has parsed it."""
    with pytest.raises(TypeError):
        chroot.add_to_store(
            "holder", b"x",
            references=[HELLO])  # type: ignore[list-item]


def test_a_path_info_is_produced_not_constructed(chroot: Store) -> None:
    """Every field comes from the store's database, so there is
    nothing a caller could correctly build one from.

    The binding says so with _produced, and the stub says NoReturn -
    which is what turns this from a runtime surprise into an error at
    the call site."""
    with pytest.raises(TypeError, match="not from a constructor"):
        PathInfo()  # type: ignore[call-arg]


def test_a_store_refuses_a_path_it_does_not_hold(chroot: Store) -> None:
    """InvalidPath, and it is a different answer from BadStorePath.

    One says the string is not a store path; this says the string is a
    perfectly good store path and this store does not have it. A
    caller can substitute or build after hearing this, and cannot
    after the other."""
    with pytest.raises(InvalidPath):
        chroot.query_path_info(StorePath(HELLO))
    with pytest.raises(BadStorePath):
        chroot.query_path_info(chroot.parse_store_path("/somewhere/else/x"))


def test_a_missing_path_is_libstores_error(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    """The shim canonicalises weakly, which does not require the path
    to exist. So the error comes from the layer that knows what the
    path was for.

    The doubled slash is upstream's, not this binding's: `nix-store
    --add /nowhere` prints it too. It comes from rendering a path in
    an accessor rooted at `/`."""
    with pytest.raises(NixError, match="does not exist"):
        chroot.add_path_to_store("nope", str(tmp_path / "missing"))


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
        chroot.add_to_store(name, name.encode(), CA.TEXT, HashAlgorithm.SHA256)

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
    so cannot get out of date. The enums exist for the editor; libstore
    stays the authority, and a str still works at runtime because a
    StrEnum member IS one."""
    with pytest.raises(UsageError, match="expect `flat`, `nar`, or `git`"):
        chroot.add_to_store(
            "x", b"y", "nonsense", HashAlgorithm.SHA256)  # type: ignore[arg-type]
    with pytest.raises(UsageError, match="unknown hash algorithm"):
        chroot.add_to_store("x", b"y", CA.TEXT, "md6")  # type: ignore[arg-type]


@pytest.mark.parametrize("method", list(CA))
def test_every_declared_method_is_a_word_libstore_knows(
        chroot: Store, method: CA) -> None:
    """The vocabulary is Nix's, so Nix is what checks it.

    A real round trip through libstore, not a table compared to
    another table. What it asserts is narrow and exact: libstore never
    answers "unknown" for a member declared here. Succeeding is one
    acceptable answer; refusing because the FEATURE is off is the
    other - `git` and `blake3` are experimental, and "disabled" says
    the word was recognised.

    It cannot catch a method Nix ADDS. C++ has no reflection, so any
    member list is hand-written wherever it lives, and this checks the
    half that can be checked: a typo, a rename, a removal."""
    try:
        chroot.add_to_store("probe", b"x", method, HashAlgorithm.SHA256)
    except NixError as e:
        assert "experimental Nix feature" in str(e), (method, str(e))


@pytest.mark.parametrize("algo", list(HashAlgorithm))
def test_every_declared_algorithm_is_a_word_libstore_knows(
        chroot: Store, algo: HashAlgorithm) -> None:
    """The same round trip, for the other vocabulary."""
    try:
        chroot.add_to_store("probe", b"x", CA.FLAT, algo)
    except NixError as e:
        assert "experimental Nix feature" in str(e), (algo, str(e))


def test_a_member_is_the_string(chroot: Store) -> None:
    """A StrEnum member IS the string, which is what keeps these a
    convenience rather than a layer.

    A typechecker asks for the enum, because that is the declared
    type and it is what makes an editor useful. At RUNTIME a plain
    string is the same call, so nothing that already works stops
    working - including a value that came from a config file."""
    assert isinstance(CA.TEXT, str) and CA.TEXT == "text"
    typed = chroot.add_to_store("same", b"x", CA.TEXT, HashAlgorithm.SHA256)
    plain = chroot.add_to_store(
        "same", b"x", "text", "sha256")  # type: ignore[arg-type]
    assert typed.to_string() == plain.to_string()


def test_bytes_are_bytes(chroot: Store) -> None:
    """A str is refused rather than encoded on the caller's behalf.

    Guessing utf-8 would put a different byte string in the store than
    the caller passed, under a name that is the hash of the guess."""
    with pytest.raises(TypeError):
        chroot.add_to_store(
            "x", "not bytes",  # type: ignore[arg-type]
            CA.TEXT, HashAlgorithm.SHA256)


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


@pytest.mark.live
def test_a_built_path_names_what_built_it(ambient_store: Store) -> None:
    """The fields a chroot store cannot show.

    A path that was ADDED has no deriver and no registration time -
    nothing built it and Nix stamps no time on it - so the hermetic
    test can only prove those two are readable. A path the daemon
    holds because something BUILT it has both, and that is the answer
    the surface exists to give.

    The environment running this is such a path, which is what makes
    the test need no fixture and no name written down. sys.prefix
    rather than sys.executable: the executable lives INSIDE a store
    path and parse_store_path takes the path itself. Asking which
    store path CONTAINS a file is a different call - nix::Store has
    toStorePath - and this binding does not offer it yet."""
    path = ambient_store.parse_store_path(sys.prefix)
    info = ambient_store.query_path_info(path)

    assert info.nar_size() > 0
    assert info.nar_hash().startswith("sha256:")
    assert info.registration_time() > 0, "a real store stamps a time"

    deriver = info.deriver()
    assert deriver is not None, "the interpreter was built, not added"
    assert deriver.is_derivation(), deriver.to_string()

    # The field that makes a store path a graph. A Python installation
    # points at libc at the very least, so this is never empty - and
    # every element is a StorePath rather than a name, which is what
    # lets a caller walk from here without parsing anything.
    references = info.references()
    assert references, "an interpreter references what it links against"
    assert all(isinstance(r, StorePath) for r in references)
    assert sorted(r.to_string() for r in references) == [
        r.to_string() for r in references], "a set's order is sorted"


def test_the_binding_initialises_libstore() -> None:
    """libstore ABORTS rather than raising when it has not been
    initialised - "The program must call nix::initNix() before calling
    any libstore library functions" - so a caller can never be the one
    to discover it. Importing the module is what runs it, and this test
    passing at all is the evidence."""
    import cythonix_bindings.store

    assert cythonix_bindings.store.Store is Store


async def test_the_async_wrapper_hands_back_an_anyio_path(
        tmp_path: pathlib.Path, source: pathlib.Path) -> None:
    """Same location, awaitable.

    The binding returns a pathlib.Path and says nothing about threads.
    The wrapper hands back an anyio.Path, because a caller who is
    already in an event loop wants to read the file without blocking
    it - and anyio.Path is a wrapper around the same value, so the two
    name the same place.

    Declared by the bindings, in _async_twins, not known by the
    codegen. Nothing about the wire changes: pathlib.Path has no
    protobuf field either way, so this is one annotation and one
    constructor call in the in-process wrapper."""
    import anyio

    from cythonix_generated import AsyncStore

    store = AsyncStore(str(tmp_path))
    path = await store.add_path_to_store("tree", str(source))
    real = await store.real_path(path)

    assert isinstance(real, anyio.Path)
    # The same place the sync binding names, and really readable.
    assert str(real) == str(Store(str(tmp_path)).real_path(path))
    assert await (real / "a.txt").read_text() == "hello\n"
    await store.aclose()
