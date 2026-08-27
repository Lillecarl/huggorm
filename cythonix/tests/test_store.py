"""
The real nix::Store (tasks/015).

Abstract in C++ and chosen by a URI, so the binding is built through
openStore rather than a constructor. "dummy://" is in-memory and needs
nothing on disk, which is what makes it testable in a build sandbox.
"""

import gc
import pathlib
import sys
from typing import Any

import pytest

from cythonix_bindings import (
    ContentAddress,
    DerivedPathBuilt,
    DrvOutput,
    Hash,
    HashAlgorithm,
    MissingPaths,
    OutputsSpec,
    PathInfo,
    Realisation,
    Signature,
    SingleDerivedPathBuilt,
    Store,
    StoreLocation,
    StorePath,
)
from cythonix_bindings import ContentAddressMethod as CA
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
        chroot: Store, source: pathlib.Path,
        tmp_path: pathlib.Path) -> None:
    """query_path_info, on a path this store just took.

    Every field comes from the store's own database rather than from
    the call that added it, so this is what the store BELIEVES, not an
    echo. The NAR size is the assertion that proves that: nothing here
    told it a size, and it is not the size of the bytes on disk."""
    path = chroot.add_path_to_store("tree", str(source))
    info = chroot.query_path_info(path)

    assert info.path() == path
    assert info.nar_size() > 0

    # A Hash, not a string that starts with "sha256:". The algorithm
    # is a FIELD, so a caller asks for it instead of splitting on a
    # colon - and the rendering is still there for printing.
    assert info.nar_hash().algorithm() == HashAlgorithm.SHA256
    assert len(info.nar_hash().digest()) == 32
    assert str(info.nar_hash()).startswith("sha256:")

    # Added, not built, so nothing derived it - and None is the answer
    # rather than a gap.
    assert info.deriver() is None
    assert info.ultimate() is False

    # None: an added path gets no registration time stamped on it.
    # Upstream keeps a time_t and spells "unknown" as 0, which is
    # also a real Unix time - so the binding reads that as None and
    # the two answers stop sharing a spelling (tasks/056). The live
    # test below is where a real time shows up.
    assert info.registration_time() is None

    # The store directory this object belongs to. On the wire because
    # nix::UnkeyedValidPathInfo cannot be built without one, and a
    # PathInfo has to rebuild on the far side (tasks/056).
    #
    # `/nix/store`, NOT the chroot. A chroot store keeps the LOGICAL
    # store directory - the prefix baked into every path it holds -
    # and puts the bytes somewhere else, which is exactly the split
    # `real_path` answers the other half of. So this is the same
    # string here and on a machine with a real /nix.
    assert info.store_dir() == "/nix/store"
    assert chroot.real_path(path) == tmp_path / "nix/store" / path.to_string()

    # Added, so content-addressed: the path is named after its own
    # bytes and says how. The live test below reads the other arm,
    # where a BUILT path has none - and that arm is why the field is
    # optional rather than a value that is sometimes empty.
    #
    # A ContentAddress, whose hash is itself a Hash: two levels of
    # nesting where the wire used to carry `fixed:r:sha256:<hash>` and
    # make every reader take it apart again.
    ca = info.ca()
    assert ca is not None
    assert ca.method() == CA.NAR
    assert ca.hash().algorithm() == HashAlgorithm.SHA256
    assert str(ca).startswith("fixed:"), ca

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
    assert info.references() == [target]

    # None and [] are the same answer, and both are the default. A
    # repeated field has no presence, so there is nothing else absence
    # could mean.
    nothing: list[list[StorePath] | None] = [None, []]
    for empty in nothing:
        alone = chroot.add_to_store("alone", b"points at nothing\n",
                                    references=empty)
        assert chroot.query_path_info(alone).references() == []


def test_a_store_reads_the_reference_graph_backwards(
        chroot: Store, source: pathlib.Path) -> None:
    """query_referrers is the inverse of PathInfo.references.

    One says what a path points at, the other says what points at it.
    Together they are the edge in both directions, which is what makes
    the store a graph a caller can walk either way - and the direction
    a garbage collector reads: a path with referrers is one something
    else still needs.

    Only a store with a database answers. nix::Store's own
    implementation raises, the way query_all_valid_paths does, because
    a substituter has no such index."""
    target = chroot.add_path_to_store("tree", str(source))
    holder = chroot.add_to_store("holder", b"points at a tree\n",
                                 references=[target])

    assert chroot.query_referrers(target) == [holder]
    # And the edge the other way round is the one PathInfo carries.
    assert chroot.query_path_info(holder).references() == [target]

    # The holder points at nothing that points back.
    assert chroot.query_referrers(holder) == []


def _chain(store: Store) -> tuple[StorePath, StorePath, StorePath]:
    """Three paths in a line: c -> b -> a.

    Built by hand because Nix does not scan for references, it is
    told them - so a chain is exactly as long as the caller says."""
    a = store.add_to_store("a", b"the end of the line\n")
    b = store.add_to_store("b", b"points at a\n", references=[a])
    c = store.add_to_store("c", b"points at b\n", references=[b])
    return a, b, c


def test_a_closure_is_the_whole_chain(chroot: Store) -> None:
    """compute_fs_closure, which is what makes one edge worth having.

    An edge is a fact; the closure is what a caller can copy, sign or
    delete as a unit. It includes the path it starts from, and it is
    TRANSITIVE - c reaches a through b, which is the whole difference
    from reading references once."""
    a, b, c = _chain(chroot)

    # A set of paths, compared as a set of paths. That reads the way
    # it does because a StorePath now hashes and compares (046).
    assert set(chroot.compute_fs_closure([c])) == {a, b, c}
    assert set(chroot.compute_fs_closure([b])) == {a, b}
    assert chroot.compute_fs_closure([a]) == [a]

    # Flipped, the same chain read the other way: what would BREAK if
    # `a` went away. That is the question a garbage collector asks.
    assert set(chroot.compute_fs_closure([a], flip_direction=True)) == {a, b, c}
    assert chroot.compute_fs_closure([c], flip_direction=True) == [c]

    # Several starting points merge into one set rather than
    # concatenating - it IS a set, so `a` appears once.
    assert set(chroot.compute_fs_closure([b, c])) == {a, b, c}
    assert chroot.compute_fs_closure([]) == []


def test_a_store_filters_the_paths_it_holds(chroot: Store) -> None:
    """query_valid_paths, the set form of is_valid_path.

    Not merely a loop over it: a store that talks to a daemon answers
    the whole set in one round trip. What comes back is shorter than
    what went in when the store is missing something, and it says
    which ones rather than how many."""
    a, b, _c = _chain(chroot)
    missing = StorePath(HELLO)

    assert set(chroot.query_valid_paths([a, b, missing])) == {a, b}
    assert chroot.query_valid_paths([missing]) == []
    assert chroot.query_valid_paths([]) == []
    assert chroot.is_valid_path(missing) is False


def test_a_store_without_a_database_cannot_read_backwards(
        store: Store) -> None:
    """The dummy store, which is what nix::Store's own implementation
    answers for."""
    with pytest.raises(Unsupported, match="not supported by store"):
        store.query_referrers(StorePath(HELLO))


def test_an_added_path_has_no_derivers(
        chroot: Store, source: pathlib.Path) -> None:
    """query_valid_derivers, and empty is a normal answer.

    A different question from PathInfo.deriver, which names the .drv
    that actually BUILT this path. Nothing built this one, so neither
    has anything to say - and the live test is where a real answer
    shows up.

    nix::Store's own implementation returns an empty set rather than
    raising, so a store that does not track this says nothing rather
    than failing. The dummy store proves that half."""
    path = chroot.add_path_to_store("tree", str(source))
    assert chroot.query_valid_derivers(path) == []
    assert chroot.query_path_info(path).deriver() is None
    assert Store("dummy://").query_valid_derivers(path) == []


def test_a_reference_must_be_a_store_path(chroot: Store) -> None:
    """The list is typed, so a string is refused before libstore sees
    it. A base name is not a StorePath even when it reads like one:
    only the class has parsed it."""
    with pytest.raises(TypeError):
        chroot.add_to_store(
            "holder", b"x",
            references=[HELLO])  # type: ignore[list-item]


def test_a_store_says_which_path_holds_a_file(
        chroot: Store, source: pathlib.Path) -> None:
    """to_store_path, which is not parse_store_path.

    `parse_store_path` takes a store path and refuses anything below
    one. A file lives below one - that is what a store object IS - so
    asking which object holds it is a second question, and this is the
    call that answers it."""
    path = chroot.add_path_to_store("tree", str(source))
    printed = chroot.print_store_path(path)

    where = chroot.to_store_path(f"{printed}/sub/b.txt")
    assert where.path() == path
    assert where.sub_path() == "/sub/b.txt"

    # The store path itself. Empty is a real answer - nothing below it
    # - rather than a gap, which is why the field carries no "?".
    assert chroot.to_store_path(printed).sub_path() == ""

    # String work only: it splits on the store DIRECTORY and never
    # touches the filesystem, so it answers for a file that is not
    # there. The store directory is the store's, not this machine's -
    # a chroot store's real files are under <root>/nix/store and this
    # still answers about /nix/store.
    assert chroot.to_store_path(f"{printed}/nope").sub_path() == "/nope"
    assert printed.startswith("/nix/store/")

    # The contrast, spelled out: the same string the other call
    # refuses.
    with pytest.raises(BadStorePath):
        chroot.parse_store_path(f"{printed}/sub/b.txt")


def test_a_file_outside_the_store_has_no_holder(chroot: Store) -> None:
    """NixError, and NOT BadStorePath, for the same fact the other
    call reports as BadStorePath.

    That is upstream's, not this binding's. StoreDirConfig::toStorePath
    throws a bare `Error` while parseStorePath throws BadStorePath, so
    a caller catching the narrow one around both calls would miss this
    one. The binding does not correct it: it would then disagree with
    `nix` for the same input, and an upstream fix would become a
    silent change here.

    Asserted rather than described, so an upstream fix reaches this
    build rather than a caller."""
    with pytest.raises(NixError, match="is not in the Nix store") as caught:
        chroot.to_store_path("/somewhere/else/x")
    assert not isinstance(caught.value, BadStorePath), (
        "upstream narrowed the type; the binding can stop warning about it")

    # The store directory itself is not IN the store either: isInStore
    # is strictly inside.
    with pytest.raises(NixError, match="is not in the Nix store"):
        chroot.to_store_path("/nix/store")


def test_a_store_follows_a_link_into_itself(
        chroot: Store, source: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """What to_store_path cannot do, and why the second call exists.

    A `result` symlink is not in the store; it POINTS at something
    that is. to_store_path splits a string and never reads the
    filesystem, so it can only refuse. This one follows the link
    first.

    Hermetic because followLinksToStore reads the LINK, not its
    target: readlink answers for a symlink whose target does not exist
    on this machine, which is exactly a chroot store's situation."""
    path = chroot.add_path_to_store("tree", str(source))
    printed = chroot.print_store_path(path)

    link = tmp_path / "result"
    link.symlink_to(printed)
    assert chroot.follow_links_to_store_path(str(link)) == path

    # The string-only call refuses the same input. That IS the
    # difference between the two, spelled out.
    with pytest.raises(NixError, match="is not in the Nix store"):
        chroot.to_store_path(str(link))

    # A path already in the store is answered without following
    # anything, so this is a superset of the other call - minus the
    # sub-path, which upstream drops here.
    assert chroot.follow_links_to_store_path(f"{printed}/sub/b.txt") == path


def test_a_store_finds_a_path_by_its_hash_part(
        chroot: Store, source: pathlib.Path) -> None:
    """The lookup a substituter makes: a bare hash back to a name.

    A store path's name begins with a 32-character base-32 hash, and
    that hash alone identifies the object - it is what a `.narinfo` is
    named after."""
    path = chroot.add_path_to_store("tree", str(source))
    hash_part = path.to_string().split("-", 1)[0]
    assert len(hash_part) == 32

    assert chroot.query_path_from_hash_part(hash_part) == path


def test_an_unknown_hash_part_is_none_not_an_error(chroot: Store) -> None:
    """None is a normal answer, and that is upstream's std::optional.

    It is a different answer from query_path_info, which raises
    InvalidPath: there the caller named a path and was wrong, here the
    caller asked whether one exists and the honest reply is no."""
    assert chroot.query_path_from_hash_part("0" * 32) is None


def test_following_a_link_keeps_the_sub_path(
        chroot: Store, source: pathlib.Path, tmp_path: pathlib.Path) -> None:
    """The half follow_links_to_store_path drops.

    A `result` symlink pointing INTO a package resolves to the file,
    not to the store object holding it. The other call answers with
    the store path alone, so this is the one that keeps `/sub/b.txt`.

    The answer is in the STORE's terms and it says so by being a str.
    A chroot store's files live under <root>/nix/store while its store
    directory stays /nix/store, so what comes back here does not exist
    on this machine - which is exactly why it is not a pathlib.Path."""
    path = chroot.add_path_to_store("tree", str(source))
    printed = chroot.print_store_path(path)

    link = tmp_path / "result"
    link.symlink_to(f"{printed}/sub/b.txt")

    resolved = chroot.follow_links_to_store(str(link))
    assert resolved == f"{printed}/sub/b.txt"
    assert isinstance(resolved, str)

    # The other call answers the other question about the same link.
    assert chroot.follow_links_to_store_path(str(link)) == path

    # And what comes back is the store's spelling, not a location: it
    # is not on this filesystem, while real_path's answer is.
    assert not pathlib.Path(resolved).exists()
    assert (chroot.real_path(path) / "sub" / "b.txt").exists()


def test_a_link_that_leads_nowhere_near_the_store_is_refused(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    """BadStorePath, the narrow type - unlike to_store_path, which
    answers the wide one for the same fact. The asymmetry is
    upstream's and this asserts both halves of it."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_text("not in any store\n")
    with pytest.raises(BadStorePath, match="is not in the Nix store"):
        chroot.follow_links_to_store_path(str(elsewhere))


def test_a_store_location_is_produced_not_constructed() -> None:
    """Only a store can perform the split, because only a store knows
    its own directory."""
    with pytest.raises(TypeError, match="not from a constructor"):
        StoreLocation()  # type: ignore[call-arg]


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

    Every element crosses as a real `nix::StorePath`. libstore answers
    with a `std::set`, which nanobind's caster turns into a Python
    list of bound objects - so nothing here is a base name that was
    printed and parsed back, and the order is the set's own.

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
    path and parse_store_path takes the path itself. to_store_path is
    the call for the other question, and the test below asks it."""
    path = ambient_store.parse_store_path(sys.prefix)
    info = ambient_store.query_path_info(path)

    assert info.nar_size() > 0
    assert info.nar_hash().algorithm() == HashAlgorithm.SHA256
    # None, not 0, when the store does not know. Upstream keeps a
    # time_t and spells "unknown" as 0 - which is also a real Unix
    # time - so the binding answers None and the test says which it
    # expects (tasks/056).
    stamped = info.registration_time()
    assert stamped is not None and stamped > 0, "a real store stamps a time"

    deriver = info.deriver()
    assert deriver is not None, "the interpreter was built, not added"
    assert deriver.is_derivation(), deriver.to_string()

    # Input-addressed: named after the derivation that made it, not
    # after its own bytes, so there is nothing to address by. None
    # rather than "", which is the distinction the wire can now carry
    # (tasks/048).
    assert info.ca() is None

    # The field that makes a store path a graph. A Python installation
    # points at libc at the very least, so this is never empty - and
    # every element is a StorePath rather than a name, which is what
    # lets a caller walk from here without parsing anything.
    references = info.references()
    assert references, "an interpreter references what it links against"
    assert all(isinstance(r, StorePath) for r in references)
    assert sorted(r.to_string() for r in references) == [
        r.to_string() for r in references], "a set's order is sorted"


@pytest.mark.live
def test_a_built_path_names_the_derivations_that_make_it(
        ambient_store: Store) -> None:
    """query_valid_derivers against a path something really built.

    Two things it can promise, and one it cannot. Every deriver is a
    .drv, always. And the deriver PathInfo names is among them WHEN
    the store still holds it - which is the whole difference between
    the two calls: PathInfo remembers what built this path, and this
    answers what could build it now. A collected .drv is remembered
    and not held.

    So the second assertion is conditional, and that is not a hedge:
    an unconditional one would fail on a machine that has run a
    garbage collection, which is most of them."""
    path = ambient_store.parse_store_path(sys.prefix)
    derivers = ambient_store.query_valid_derivers(path)

    assert all(d.is_derivation() for d in derivers), [
        d.to_string() for d in derivers]

    deriver = ambient_store.query_path_info(path).deriver()
    assert deriver is not None
    if ambient_store.is_valid_path(deriver):
        assert deriver.to_string() in [d.to_string() for d in derivers]


@pytest.mark.live
def test_a_real_file_names_the_path_that_holds_it(
        ambient_store: Store) -> None:
    """The question sys.executable asks and parse_store_path cannot
    answer.

    The interpreter running this lives at `<store path>/bin/python3.x`
    - a file in a store object, not a store object. The ambient store
    is what makes this real: its store directory is the machine's, so
    the split lands on a path that exists."""
    where = ambient_store.to_store_path(sys.executable)

    assert where.sub_path().startswith("/bin/")
    assert where.path().to_string().endswith(
        pathlib.Path(sys.prefix).name), where.path().to_string()

    # And it round-trips against the other direction: the store path
    # plus the sub-path is where the file really is.
    assert (ambient_store.real_path(where.path())
            / where.sub_path().lstrip("/")) == pathlib.Path(sys.executable)


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


# --- realisations ---------------------------------------------------


def test_a_store_with_no_ca_derivations_realises_nothing(
        chroot: Store) -> None:
    """None, and it is the store's answer rather than a gap.

    `ca-derivations` is an experimental feature and it is off here, so
    `LocalStore::queryRealisationUncached` hands back nothing for EVERY
    id without consulting a mapping. That is a real answer - this store
    cannot tell you - and it is a different fact from "that output was
    never realised". Nothing in the API separates the two, and this
    test pins the shape rather than pretending it does.

    Asked with a real key, not a null one: the point is that a
    well-formed question gets None, not that a malformed one does."""
    key = DrvOutput(Hash(HashAlgorithm.SHA256, bytes(32)), "out")
    assert chroot.query_realisation(key) is None


def test_a_realisation_key_is_two_facts_and_a_rendering() -> None:
    """DrvOutput is the key a caller BUILDS, so it is constructible.

    The hash is a `Hash` rather than a string, which is what lets a
    caller ask for the algorithm instead of splitting `to_string` on a
    colon - and `to_string` is upstream's own `<hash>!<output>`, with
    the hash base16 and prefixed, which is what `DrvOutput::parse`
    reads back."""
    key = DrvOutput(Hash(HashAlgorithm.SHA256, bytes(range(32))), "dev")

    assert key.drv_hash().algorithm() == HashAlgorithm.SHA256
    assert key.output_name() == "dev"

    # Upstream's spelling, and the two halves are visible in it. The
    # hash carries its algorithm here where `Hash.base16()` does not:
    # `DrvOutput::to_string` calls `strHash()`, which is base16 WITH
    # the prefix, because `DrvOutput::parse` reads it back.
    assert str(key) == f"sha256:{key.drv_hash().base16()}!dev"
    assert str(key).startswith("sha256:000102")
    assert str(key).endswith("!dev")

    # It compares as its C++ does, over exactly the two members the
    # wire carries - so equal keys hash alike and a set of them
    # deduplicates.
    same = DrvOutput(Hash(HashAlgorithm.SHA256, bytes(range(32))), "dev")
    assert key == same and hash(key) == hash(same)
    assert len({key, same}) == 1
    assert key != DrvOutput(Hash(HashAlgorithm.SHA256, bytes(range(32))), "out")


def test_a_realisation_compares_on_its_signatures_where_nix_does_not(
        chroot: Store) -> None:
    """A deliberate divergence, pinned so it cannot drift back.

    `nix::UnkeyedRealisation` compares on `outPath` alone - upstream
    writes GENERATE_CMP over that one member and comments "TODO
    sketchy that it avoids signatures" - and nix::Realisation's
    defaulted operators inherit it. So in C++ two of these differing
    only in who signed them are EQUAL.

    The binding cannot follow that. `__hash__` is derived from the
    declared parts and `signatures` is one of them, so two objects
    that compared equal would hash differently - which breaks the one
    invariant Python asks of a value. Equality is over the parts here,
    and this is the case where that shows."""
    held = chroot.add_to_store("realised", b"x", CA.NAR, HashAlgorithm.SHA256)
    key = DrvOutput(Hash(HashAlgorithm.SHA256, bytes(32)), "out")

    bare = _rebuild(Realisation, key, held, [])
    signed = _rebuild(Realisation, key, held, [Signature("k", bytes(64))])

    # Same id, same path, different signatures. libstore says equal.
    assert bare.id() == signed.id()
    assert bare.out_path() == signed.out_path()
    assert bare != signed, "the binding compares signatures; libstore does not"
    assert hash(bare) != hash(signed)

    # ...and the invariant that forces it.
    same = _rebuild(Realisation, key, held, [Signature("k", bytes(64))])
    assert same == signed and hash(same) == hash(signed)


# --- sum types ------------------------------------------------------


def test_an_outputs_spec_refuses_the_two_states_that_blur_its_arms(
) -> None:
    """`all` is a TAG, not "no names", and the pair is checked.

    Upstream is a `variant<All, Names>`, so the two are exclusive by
    construction. Here they are two fields, because one arm carries
    nothing and protobuf spells that `bool` - which makes the invalid
    pairs representable and puts the burden on the constructor.

    Both are refused, in libstore's own error type. Upstream deletes
    the default constructor to force the choice and asserts `Names` is
    non-empty; these are the same two states."""
    with pytest.raises(UsageError, match="names nothing"):
        OutputsSpec(all=True, names=["out"])
    with pytest.raises(UsageError, match="at least one"):
        OutputsSpec(all=False, names=[])
    with pytest.raises(UsageError, match="at least one"):
        OutputsSpec()

    # ...and the two valid ones, which say one thing each.
    assert OutputsSpec(all=True).all() is True
    assert OutputsSpec(all=True).names() == []
    assert OutputsSpec(names=["dev", "out"]).all() is False
    assert OutputsSpec(names=["dev", "out"]).names() == ["dev", "out"]


def test_a_blurred_outputs_spec_does_not_get_PAST_the_wire() -> None:
    """The refusal is on the decode path, not only on the caller's.

    Two fields where upstream has a variant means a MESSAGE can carry
    `all` and names together - every wire format can spell garbage.
    What matters is whether garbage gets past the boundary, and the
    only thing standing there is the constructor.

    So this asks the codec directly, with parts no caller could have
    made. `_from_parts` IS the constructor for a value the emitter
    binds one for, which is what makes the refusal cover both
    directions - and this pins it, because nothing else would notice
    if a future `_from_parts` stopped going through it."""
    made: Any = OutputsSpec
    with pytest.raises(UsageError, match="names nothing"):
        made._from_parts(True, ["out"])
    with pytest.raises(UsageError, match="at least one"):
        made._from_parts(False, [])


def test_a_derived_path_is_printed_and_parsed_BY_THE_STORE(
        chroot: Store) -> None:
    """Rendering needs a store, so it is the store that does it.

    `DerivedPath::to_string` takes a StoreDirConfig by upstream's own
    signature - the printed form carries the store's directory - so
    the type cannot print itself and there is no `__str__`. That is
    040's split, and it is the same one `print_store_path` already
    makes for a StorePath.

    The `^` spelling, not the `!` one. Upstream keeps both; `^` is
    what the command line takes."""
    drv = chroot.add_to_store("drv", b"x", CA.NAR, HashAlgorithm.SHA256)
    printed = chroot.print_store_path(drv)

    # The opaque arm IS a StorePath, which is why a caller never
    # learns a wrapper class exists.
    assert chroot.print_derived_path(drv) == printed

    named = DerivedPathBuilt(drv, OutputsSpec(names=["dev", "out"]))
    assert chroot.print_derived_path(named) == f"{printed}^dev,out"

    every = DerivedPathBuilt(drv, OutputsSpec(all=True))
    assert chroot.print_derived_path(every) == f"{printed}^*"

    # ...and back, which is what makes the pair a pair. Each comes
    # back as the ARM it was, not as a wrapper around one.
    assert chroot.parse_derived_path(printed) == drv
    assert isinstance(chroot.parse_derived_path(printed), StorePath)
    for target in (named, every):
        back = chroot.parse_derived_path(chroot.print_derived_path(target))
        assert back == target
        assert isinstance(back, DerivedPathBuilt)


def test_a_store_says_what_building_these_would_have_to_do(
        chroot: Store) -> None:
    """query_missing, on a store that holds what is asked for.

    Nothing is built, fetched or locked. A path already valid here is
    not MISSING, so it appears in none of the three lists - which is
    what makes an all-empty answer mean "there is nothing to do"
    rather than "I did not look".

    This is the first method taking a UNION, and it takes a list of
    them - so the opaque arm crosses as a plain StorePath and the
    caller writes what they already hold."""
    held = chroot.add_to_store("held", b"x", CA.NAR, HashAlgorithm.SHA256)

    nothing = chroot.query_missing([held])
    assert nothing.will_build() == []
    assert nothing.will_substitute() == []
    assert nothing.unknown() == []
    assert nothing.download_size() == 0
    assert nothing.nar_size() == 0

    # An empty ask is a real ask, and answers the same way.
    assert chroot.query_missing([]).unknown() == []


def test_a_union_nested_past_the_limit_is_refused_by_name() -> None:
    """The wire is a trust boundary, and recursion has no natural end.

    A SingleDerivedPath's built arm holds another SingleDerivedPath, so
    a peer can send a chain as long as it likes. Without a cap the
    answer is Python's own RecursionError, which reaches a caller as an
    anonymous InternalError - true, and useless.

    The limit is in the CODEC rather than in a declaration, because
    depth is a fact about crossing rather than about the type. It is
    generous: anything real is one or two deep, so only a bug or an
    attack sees this."""
    import json

    from google.protobuf import message_factory

    import cythonix_generated as flg
    from cythonix.grpc_pb import load_pool
    from cythonix.wire import WireCodec
    from cythonix_generated._wiretypes import MAX_UNION_DEPTH

    manifest = json.loads(
        (pathlib.Path(flg.__file__).parent / "manifest.json").read_text())
    codec = WireCodec(manifest)
    pool = load_pool()

    def message() -> Any:
        # protobuf ships no stubs for either call, and both are the
        # ordinary way to reach a message class from a pool.
        kls = message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
            pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
                "cythonix.v1.SingleDerivedPathMsg"))
        return kls()

    # A chain one deeper than the cap, built from the inside out.
    deep: Any = StorePath(HELLO)
    for _ in range(MAX_UNION_DEPTH + 1):
        deep = SingleDerivedPathBuilt(deep, "out")

    msg = message()
    with pytest.raises(ValueError, match=str(MAX_UNION_DEPTH)):
        codec.union_to_msg("SingleDerivedPath", deep, msg)

    # ...and one at the cap crosses, so the limit is a limit rather
    # than a refusal of the whole shape.
    fine: Any = StorePath(HELLO)
    for _ in range(MAX_UNION_DEPTH - 1):
        fine = SingleDerivedPathBuilt(fine, "out")
    ok = message()
    codec.union_to_msg("SingleDerivedPath", fine, ok)
    assert codec.union_from_msg("SingleDerivedPath", ok) == fine


# --- the wire-value round trip --------------------------------------


def _wire_values() -> dict[str, str]:
    """Every type the manifest says crosses as its PARTS."""
    import json

    import cythonix_generated as flg

    manifest = json.loads(
        (pathlib.Path(flg.__file__).parent / "manifest.json").read_text())
    return {name: entry["module"]
            for group in ("wrappers", "returned_types")
            for name, entry in manifest[group].items()
            if entry["wire"] == "value"}


def _rebuild(kind: Any, *parts: Any) -> Any:
    """`_from_parts`, reached the one way a typechecker allows.

    The emitter skips every `_`-prefixed name when it writes the
    stubs, so `Realisation._from_parts` is invisible to mypy and
    should be: it is not surface. The round trip is exactly the
    contract it exists for, so one `Any` here buys the test its only
    way to build a PRODUCED value from parts."""
    return kind._from_parts(*parts)


def test_every_wire_value_survives_its_own_round_trip(
        chroot: Store, tmp_path: pathlib.Path) -> None:
    """A wire value must rebuild from the parts it hands over.

    `_parts()` renders one; `_from_parts(*parts)` rebuilds it. The two
    are inverses or the type does not cross correctly, and nothing
    else in the build proves that.

    It used to be true BY CONSTRUCTION. A produced value was a struct
    the emitter declared, whose members were the wire types, so
    `_from_parts` was aggregate initialisation and could not disagree
    with `_parts`. Once a value binds a REAL Nix type, the two become
    a hand-written bijection - an accessor renders a hash, a
    `_from_parts` parses it back - and a body that forgets a field
    compiles and zero-initialises it in silence.

    So the round trip stops being true by construction and has to be
    proven. This is where (tasks/056).

    TWO cases per type, not one, and the second is the one that
    works. A single sample proves nothing about a part that happens to
    hold its C++ default: deleting `u.ultimate = ultimate` from
    PathInfo's body passed this test, because the only PathInfo it
    built came from an ADDED path, whose `ultimate` is already false.
    The same shadow covered `deriver`, `registration_time`,
    `references` and `sigs` - five of ten parts asleep. So each type
    lists a second case whose every part differs, and the test refuses
    to pass while any part holds one value across all of them.

    SAMPLES is the one hand-written part, and the test refuses to pass
    if it does not cover every wire value the manifest declares.
    Adding a value without adding a sample fails here rather than
    shipping unproven.

    WHAT IT STILL DOES NOT PROVE, and cannot here: a hand-written case
    goes parts -> object -> parts, so it exercises the PARSE and the
    render of a state no producer in this suite reaches. A PathInfo
    with `ultimate=True` has never been rendered off an object a store
    actually made, because a hermetic store cannot build one. That
    wants the live suite, and it is a gap in coverage rather than a
    hole in this gate."""
    from cythonix_bindings import MockDerivedPath, MockLocalStore

    mock = MockLocalStore()
    mock_path = mock.add_text_to_store("round-trip", "x")
    other_mock = mock.add_text_to_store("round-trip-other", "y")
    held = chroot.add_to_store("round-trip", b"x", CA.NAR, HashAlgorithm.SHA256)
    other = chroot.add_to_store("other", b"yy", CA.NAR, HashAlgorithm.SHA256)
    info, other_info = (chroot.query_path_info(p) for p in (held, other))

    # A PathInfo with nothing at its default. Every part is a real
    # value libstore will parse - the hash and the content address
    # come from a path this store actually holds - so the only thing
    # made up is which value goes where.
    #
    # NO TWO PARTS SHARE A VALUE, and that is load-bearing rather than
    # tidy. It is what catches a body that SWAPS two fields - writing
    # `u.narSize = registration_time` drops nothing and survives every
    # check above, and comes back visibly wrong here. Give two parts
    # the same value and swaps between those two go dark.
    #
    # `sorted`, because references cross in the order libstore's own
    # std::set keeps them and the binding's `<` is that same
    # operator<. An unsorted list would come back sorted and the round
    # trip would read as broken when it is not.
    populated: tuple[Any, ...] = (
        other,                          # path
        "/other/store",                 # store_dir
        other_info.nar_hash(),          # nar_hash
        info.nar_size() + 1,            # nar_size
        held,                           # deriver
        1_700_000_000,                  # registration_time
        True,                           # ultimate
        other_info.ca(),                # ca
        sorted([held, other]),          # references
        [Signature("key-1", bytes(64))],  # sigs
    )

    # `Any`, because `_parts` and `_from_parts` are private: the
    # emitter skips every `_`-prefixed name when it writes the stubs,
    # so a typechecker cannot see them and should not. The round trip
    # is exactly the contract those two exist for, so this is the one
    # place that reaches past the public surface on purpose.
    #
    # The FIRST entry of each list is a real object, because the round
    # trip has to start from something a producer actually made. The
    # rest are parts tuples, which is what lets a case reach a state
    # no hermetic producer here can reach.
    samples: dict[str, tuple[Any, list[tuple[Any, ...]]]] = {
        # Two algorithms and two digests. A hash is the one wire value
        # a CALLER builds - it is what you have when you have read one
        # from somewhere Nix did not print it - so both cases are
        # constructed, and neither is a producer's answer.
        "Hash": (Hash(HashAlgorithm.SHA256, bytes(range(32))),
                 [(HashAlgorithm.SHA1, bytes(range(20)))]),
        # A value with a value inside it. The second case differs in
        # BOTH halves, so neither the method nor the nested hash can
        # be dropped without this saying which.
        "ContentAddress": (
            ContentAddress(CA.NAR, Hash(HashAlgorithm.SHA256,
                                        bytes(range(32)))),
            [(CA.FLAT, Hash(HashAlgorithm.SHA1, bytes(range(20))))]),
        "Signature": (Signature("cache.nixos.org-1", bytes(range(64))),
                      [("builder-2", bytes(range(1, 65)))]),
        # A CA derivation's output, and what it turned out to be. Both
        # constructed: a hermetic store answers None for every id,
        # because `ca-derivations` is off and there is no mapping to
        # consult - so nothing here can produce a real one, and the
        # live test says so rather than this pretending.
        "DrvOutput": (DrvOutput(Hash(HashAlgorithm.SHA256, bytes(32)), "out"),
                      [(Hash(HashAlgorithm.SHA1, bytes(20)), "dev")]),
        "Realisation": (
            _rebuild(Realisation,
                     DrvOutput(Hash(HashAlgorithm.SHA256, bytes(32)), "out"),
                     held, []),
            [(DrvOutput(Hash(HashAlgorithm.SHA1, bytes(20)), "dev"),
              other, [Signature("k", bytes(64))])]),
        # The SUM types. `drv_path` is a union, so the second case
        # takes the OTHER arm - and for the Single one that arm is
        # another SingleDerivedPathBuilt, which is the recursion.
        # Sorted, because names come back in the std::set's order and
        # `_parts` never produces them any other way.
        "OutputsSpec": (OutputsSpec(all=True), [(False, ["dev", "out"])]),
        "SingleDerivedPathBuilt": (
            SingleDerivedPathBuilt(held, "out"),
            [(SingleDerivedPathBuilt(other, "dev"), "man")]),
        "DerivedPathBuilt": (
            DerivedPathBuilt(held, OutputsSpec(all=True)),
            [(SingleDerivedPathBuilt(other, "out"),
              OutputsSpec(all=False, names=["dev"]))]),
        # Five fields, and the second case differs in every one -
        # including the two widths, which a swap would otherwise hide.
        "MissingPaths": (
            _rebuild(MissingPaths, [held], [], [other], 1, 2),
            [([], [other], [held, other], 3, 4)]),
        "StorePath": (held, [(other.to_string(),)]),
        "PathInfo": (info, [populated]),
        "StoreLocation": (
            chroot.to_store_path(chroot.print_store_path(held)),
            [(other, "/bin/sh")]),
        "MockStorePath": (mock_path, [(other_mock.to_string(),)]),
        "MockDerivedPath": (
            MockDerivedPath(mock_path, "out"),
            [(other_mock, None)]),
    }

    declared = _wire_values()
    missing = sorted(set(declared) - set(samples))
    assert not missing, (
        f"the manifest declares {missing} as wire values and this test "
        f"cannot build one. Add a sample - the round trip is not "
        f"optional for a type that crosses as its parts.")

    for name in sorted(declared):
        built, extra = samples[name]
        fields = [f for f, _ in type(built)._wire_fields]
        cases = [built._parts(), *extra]
        for parts in cases:
            back = type(built)._from_parts(*parts)._parts()
            lost = [f for f, sent, got in zip(fields, parts, back, strict=True)
                    if sent != got]
            assert not lost, (
                f"{name} does not survive its own round trip. "
                f"{lost} changed:\n"
                f"  sent:      {parts}\n"
                f"  came back: {back}")
        # ...and the rebuilt one must BE equal, not merely carry the
        # same parts. A type whose __eq__ reads something the parts do
        # not would pass the line above and fail a caller.
        assert type(built)._from_parts(*cases[0]) == built, (
            f"{name} rebuilt unequal to the original")

        # No part may hold one value across every case. A part that
        # does is a part this test cannot see, however many times it
        # round-trips - which is what let a dropped `ultimate` pass.
        asleep = [fields[i] for i, column in
                  enumerate(zip(*cases, strict=True))
                  if len({repr(v) for v in column}) < 2]
        assert not asleep, (
            f"{name}: {asleep} holds one value in every case, so this "
            f"test would pass with it dropped from _from_parts. Give a "
            f"case where it differs.")


@pytest.mark.live
def test_a_real_store_object_survives_its_own_round_trip(
        ambient_store: Store) -> None:
    """The half the hermetic gate cannot reach: a REAL object.

    The gate above builds its second PathInfo from a hand-written
    parts tuple, so for every state a chroot store cannot produce -
    a deriver, a registration time, a signature - it proves the PARSE
    and not the render. Nothing had ever rendered those off an object
    a store actually made (tasks/056).

    This does. `sys.prefix` is a path something BUILT, so the store's
    own answer carries what an added path does not, and the round trip
    runs over that answer rather than over a tuple this file wrote.

    It asserts what it relies on, and only what holds on ANY machine.
    A deriver and a registration time are there whether the path was
    built here or substituted; `ultimate` and `sigs` are the opposite
    pair - built here gives ultimate and no signature, substituted
    gives signatures and no ultimate - so this exercises one of the
    two and cannot say which. Both are states a chroot store cannot
    reach, which is the point.

    It OVERLAPS the hermetic gate by design, and loses the race to it
    on purpose: drop a field from `_from_parts` and the sandbox gate
    fails first, because the cheaper gate should. What is left over is
    the part only this one has - the object is real."""
    path = ambient_store.parse_store_path(sys.prefix)
    built: Any = ambient_store.query_path_info(path)

    # The states a hermetic store cannot make. Without these the test
    # would pass on a value no richer than the one already covered.
    assert built.deriver() is not None
    assert built.registration_time() is not None

    parts = built._parts()
    fields = [f for f, _ in type(built)._wire_fields]
    back = type(built)._from_parts(*parts)._parts()
    lost = [f for f, sent, got in zip(fields, parts, back, strict=True)
            if sent != got]
    assert not lost, (
        f"a store's own PathInfo loses {lost} on the round trip:\n"
        f"  sent:      {parts}\n"
        f"  came back: {back}")
