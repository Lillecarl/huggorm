"""
Gates over the DECLARATION reader and the emitters that read a tree.

Nothing here builds or imports a binding. What it drives is the half
of the build that turns a declaration into a document, on declarations
written for the test - so it can state a case the real corpus does not
have, and does not have to invent one there.

A version branch is that case. `read.py` imports every declaration so
Python resolves `if NIX_VERSION >= ...`, and the errors emitter parsed
the file a second time and saw neither arm (tasks/073). No declaration
in this repo branches today, so nothing real can hold the fix.
"""

import ast
import pathlib
from collections.abc import Callable

import pytest

BRANCHED = '''"""Two exception classes, one behind a version."""

from huggorm_dsl.declare import NIX_VERSION


class NixError(Exception):
    """The base every other one derives from."""

    cxx = "nix::Error"


if NIX_VERSION >= (2, 0):

    class Here(NixError):
        """The arm this build has - the version is long past."""

        cxx = "nix::Here"

else:

    class Gone(NixError):
        """The arm it does not have."""

        cxx = "nix::Gone"
'''

# The same hierarchy with nothing to resolve. Written out rather than
# derived from the one above: the two differ in exactly the thing
# under test, and computing one from the other would hide it.
FLAT = '''"""Two exception classes, neither behind a version."""


class NixError(Exception):
    """The base every other one derives from."""

    cxx = "nix::Error"


class Here(NixError):
    """Beside it, not under an `if`."""

    cxx = "nix::Here"
'''


def _declaration(tmp_path: pathlib.Path, source: str) -> str:
    """One declaration on disk, as a path its reader can open.

    A fresh directory per call. `read.load` caches an imported
    declaration by PATH, so two tests writing different text to one
    name would read the first one twice."""
    out = tmp_path / "errs.py"
    out.write_text(source)
    return str(out)


def test_a_version_branch_is_resolved_before_an_emitter_sees_it(
        tmp_path: pathlib.Path) -> None:
    """The class the import kept reaches every output; the other
    reaches none.

    Three outputs come off an exception declaration - the manifest
    entries, the translator's catch chain, and the emitted module -
    and before this they disagreed about what a branch means. The
    entries and the chain read the top level, where a branched class
    is not; the module copied the whole document, so it emitted both
    the class and the `if` around it.

    So `Gone` must be absent from all three and `Here` present in all
    three. Asserting only one direction would pass on an emitter that
    kept everything."""
    from huggorm_dsl.read import load, resolved
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    tree = resolved(path)

    entries = pyerrors.entries(tree, load(path))
    assert sorted(entries) == ["Here", "NixError"]
    # ...and it inherits, which is the half only the IMPORT knows.
    assert entries["Here"]["bases"] == ["NixError"]

    chain = "\n".join(pyerrors.chain(tree, "raise_as", "pkg.errors"))
    assert "nix::Here" in chain
    assert "nix::Gone" not in chain
    # Most-derived first, or the base swallows the subclass.
    assert chain.index("nix::Here") < chain.index("nix::Error")

    emitted = pyerrors.module(tree, "emitted")
    assert "class Here" in emitted
    assert "Gone" not in emitted


def test_the_emitted_module_keeps_no_trace_of_the_branch(
        tmp_path: pathlib.Path) -> None:
    """Three things must not survive into the emitted exception module.

    The `if` itself, because the arm is chosen at build time and a
    condition left in the output would have to be evaluated again by
    something that cannot answer it.

    The `cxx` line, which names C++ no Python caller can act on. It
    was stripped from a top-level class and not from a branched one,
    because the strip walked the top level only.

    And the import of the declaration LANGUAGE. A branch has to spell
    `NIX_VERSION` to be written at all, and once the arm is chosen
    that name is unreferenced - so carrying the import would make the
    emitted package depend on a build-time one, and would fail
    `test_no_unused_imports` besides."""
    from huggorm_dsl.read import resolved
    from huggorm_gen.cppgen import pyerrors

    emitted = pyerrors.module(resolved(_declaration(tmp_path, BRANCHED)),
                              "emitted")
    tree = ast.parse(emitted)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.If)]
    assert "cxx" not in emitted
    assert "huggorm_dsl" not in emitted
    assert not [n for n in tree.body
                if isinstance(n, ast.Import | ast.ImportFrom)]


def test_the_errors_emitter_refuses_a_tree_nothing_resolved(
        tmp_path: pathlib.Path) -> None:
    """A raw parse is the wrong tree, and saying so is the gate.

    This is what catches a caller reverting to `Corpus.tree`, and
    nothing else can: for a declaration that does not branch the two
    trees are identical, and no declaration in this repo branches.
    So the emitter refuses the shape a raw parse has and a resolved
    one cannot - a surviving `ast.If`.

    All three readings, because all three used to walk the body on
    their own and that is how they came to disagree."""
    from huggorm_dsl.read import DeclarationError, load
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    raw = ast.parse(pathlib.Path(path).read_text())
    mod = load(path)

    readings: list[Callable[[], object]] = [
        lambda: pyerrors.entries(raw, mod),
        lambda: pyerrors.chain(raw, "raise_as", "pkg.errors"),
        lambda: pyerrors.module(raw, "emitted"),
    ]
    for reading in readings:
        with pytest.raises(DeclarationError, match="version branch"):
            reading()


def test_a_declaration_that_does_not_branch_reads_the_same_either_way(
        tmp_path: pathlib.Path) -> None:
    """Resolving costs nothing where there is nothing to resolve.

    The claim the fix rests on: `read.resolved` is a raw parse for
    every declaration in this repo, so pointing the emitter at it
    changed no output. Measured rather than assumed, because "it
    should be a no-op" is exactly the kind of claim that is wrong."""
    from huggorm_dsl.read import resolved
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, FLAT)
    raw = ast.parse(FLAT)
    assert pyerrors.module(resolved(path), "emitted") == \
        pyerrors.module(raw, "emitted")


# A bound class with one accessor written as an ATTRIBUTE. The word is
# Python's own and the reader keeps it (`Method.prop`); no emitter
# honours it. Written here because no declaration in the corpus uses
# it, which is how the emitter path behind it went unread from the day
# it was written until it was deleted (tasks/075).
ATTRIBUTE = '''"""One bound class whose accessor is a @property."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    @property
    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("""
return self.to_string(nix::HashFormat::Base16, /*includeAlgo=*/false);
        """)
'''


def test_the_reader_sees_an_accessor_the_import_kept_as_a_descriptor(
        tmp_path: pathlib.Path) -> None:
    """A `@property` accessor survives the read, and says it is one.

    `_live` asks each definition the import kept for its
    `co_firstlineno`, and a `property` object has no `__code__` to ask
    - so the accessor named no live line and `_resolve` dropped its
    node as if a version branch had. Silently.

    Measured before the fix, on `PathInfo.registration_time` marked
    `@property`: the accessor was gone from the emitted binding, from
    `_parts`, from `__repr__`, from `__hash__` and from
    `_wire_fields`, and the build's only complaint came from the
    hand-written `_from_parts` body that still named it -
    `pathinfo.cpp:99: 'registration_time' was not declared in this
    scope`. A class whose `_from_parts` the emitter derives would have
    lost the field with no diagnostic at all.

    Both halves are asserted. That the accessor is there, and that it
    still reads as a property - a reader that kept it by forgetting
    what it is would pass the first and fail the class below."""
    from huggorm_dsl.read import read

    path = _declaration(tmp_path, ATTRIBUTE)
    cls = read(path).classes[0]
    assert [m.name for m in cls.methods] == ["base16"]
    assert cls.methods[0].prop


# One value carrying both 64-bit widths. Written here because the
# corpus has each width but never both on one class, and the fact
# under test is the DIFFERENCE between them (tasks/079).
WIDTHS = '''"""One value that carries both 64-bit widths."""

from huggorm_dsl.declare import (
    I64,
    U64,
    Cxx,
    binding,
    header,
    wire_value,
)


@header("nix/store/path-info.hh")
@binding(cxx="nix::Sizes", threading="pool", blocking=False)
@wire_value()
class Sizes:
    """Two numbers, one of each width."""

    def total(self) -> U64:
        """How many bytes there are."""
        Cxx("return self.total;")

    def when(self) -> I64:
        """When it happened, as a Unix time."""
        Cxx("return self.when;")
'''

# The same width inside a CONTAINER. `type_of` attaches the alias's
# C++ spelling to the whole `dict[str, U64]`, so a reader that took
# it at face value would call the dict a uint.
HELD = '''"""One value whose field is a container of a width."""

from huggorm_dsl.declare import U64, Cxx, binding, header, wire_value


@header("nix/store/path-info.hh")
@binding(cxx="nix::Sizes", threading="pool", blocking=False)
@wire_value()
class Sizes:
    """One number per name."""

    def by_name(self) -> "dict[str, U64]":
        """How many bytes each one holds."""
        Cxx("return self.by_name;")
'''

# A PROXY whose method takes the unsigned width. A proxy is reached
# through a service, so this parameter is a message field - and the
# manifest has one string for it, read as a Python annotation by the
# stubs and as a wire type by the schema.
TAKES = '''"""One proxy whose method takes a width."""

from huggorm_dsl.declare import U64, Bint, Cxx, binding, header


@header("nix/store/store-api.hh")
@binding(cxx="nix::Sizes", threading="pool", blocking=True)
class Sizes:
    """A thing a caller keeps a handle on."""

    def fits(self, limit: U64) -> Bint:
        """Whether it fits under the limit."""
        Cxx("return self.fits(limit);")
'''


def test_a_field_says_which_64_bit_integer_it_is(
        tmp_path: pathlib.Path) -> None:
    """`U64` crosses as `uint` and `I64` as `int`, and Python sees
    `int` for both.

    One proto type carried every integer before this, and it was
    `sint64` - which holds every int64_t and half of a uint64_t. The
    half it does not hold is not hypothetical: upstream spells "no
    limit" as the largest uint64_t, so `GCOptions()` - the DEFAULT -
    could not cross an RPC at all:

        ValueError: Value out of range: 18446744073709551615

    The width was already in the declaration, in the C++ spelling the
    alias carries. `Type.wire` threw it away.

    Both halves are asserted. The wire tells the two apart, and the
    manifest's Python spelling does NOT - a caller holds an `int`
    either way, and a stub that said `uint` would name a type Python
    does not have."""
    from huggorm_dsl.read import read
    from huggorm_gen.cppgen import manifest

    cls = read(_declaration(tmp_path, WIDTHS)).classes[0]
    assert [(f.name, f.type) for f, _ in cls.parts] == [
        ("total", "uint"), ("when", "int")]

    entry = manifest.entry(cls, "pkg", "mod")
    assert entry["wire_fields"] == [["total", "uint"], ["when", "int"]]
    assert [(m["name"], m["return_type"]) for m in entry["methods"]] == [
        ("total", "int"), ("when", "int")]


def test_a_container_of_a_width_is_refused_rather_than_guessed(
        tmp_path: pathlib.Path) -> None:
    """`dict[str, U64]` has no wire spelling, and says so.

    The reader attaches an alias's C++ spelling to the whole
    annotation, so the uint64_t on this field describes what the dict
    HOLDS. Naming the dict `uint` would be wrong and calling it a
    plain `dict[str, int]` would lose the top half of every value in
    it - so it refuses, which is the only one of the three that
    cannot be silently wrong.

    No declaration writes one today. That is why it is written here:
    the branch would otherwise be unread."""
    from huggorm_dsl.read import read

    cls = read(_declaration(tmp_path, HELD)).classes[0]
    with pytest.raises(TypeError, match="tasks/079"):
        _ = cls.parts


def test_a_service_refuses_a_parameter_whose_width_it_cannot_spell(
        tmp_path: pathlib.Path) -> None:
    """A proxy method may not take a `U64`.

    A FIELD says its own width; a parameter does not. `params[].type`
    is one string, read by the stub emitter as a Python annotation and
    by the schema builder as a wire type, and `int` cannot be both
    right for the first and right for the second.

    Refused at build time rather than carried, because carrying it
    means a second key beside `type` that `model.py` cannot reflect
    off a compiled class - so `check.py` would diff the manifest
    against a shape reflection has no way to produce. Nothing declares
    such a parameter, so the refusal costs nothing and the wrong
    answer would have been a silently truncated number."""
    from huggorm_dsl.read import read
    from huggorm_gen.cppgen import manifest

    cls = read(_declaration(tmp_path, TAKES)).classes[0]
    assert not cls.decl.wire, "a plain @binding is a proxy"
    with pytest.raises(TypeError, match="tasks/079"):
        manifest.entry(cls, "pkg", "mod")


def test_the_binding_refuses_an_accessor_declared_as_an_attribute(
        tmp_path: pathlib.Path) -> None:
    """`@property` is refused, because a binding alone cannot honour it.

    Three other outputs read an accessor as a CALL: the emitted
    `_parts`, `__repr__` and `__hash__` write `h.attr(name)()`, the
    stub writes `def name(self)`, and the wire reads the parts the way
    `_parts` sends them. A `def_prop_ro` here would make this file the
    only one that agreed with the declaration.

    The refusal replaced an `_accessor` function that emitted exactly
    that, and that no declaration had ever reached - which is why its
    two-row table of optional return spellings was never seen to be
    wrong (tasks/075)."""
    from huggorm_dsl.read import read
    from huggorm_gen.cppgen import nbemit

    cls = read(_declaration(tmp_path, ATTRIBUTE)).classes[0]
    with pytest.raises(TypeError, match="ATTRIBUTE"):
        nbemit.bind_function(cls, {cls.name: cls})


def _corpus_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    """A directory shaped like `decl/`: two declarations and two
    things that are not one.

    The README and the `__pycache__` are the point of the second
    pair. The census globs `*.py`, so neither needs a rule of its
    own - and a census that grew an exclusion list would be a second
    statement of what a declaration is."""
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "README.md").write_text("")
    (tmp_path / "__pycache__").mkdir()
    return tmp_path


def test_a_declaration_in_no_list_fails_the_build(
        tmp_path: pathlib.Path) -> None:
    """A file nobody lists is an error, not a silence.

    `decl/gc.py` was written, was well-formed, imported, parsed and
    emitted nothing at all: `nix build bindings-src` succeeded and
    wrote no `gc.cpp`, because the name was not in `NANOBIND`
    (tasks/074). The failure looked exactly like a declaration with
    no classes in it.

    Third silent drop in three tasks, after a version-branched class
    (073) and a `@property` accessor (075). The pattern is that an
    emitter SKIPS what it does not recognise, and a skip reads as an
    absence."""
    from huggorm_decl import census

    root = _corpus_dir(tmp_path)
    census(root, (("ONE", ("a.py",)), ("TWO", ("b.py",))))

    with pytest.raises(TypeError, match="in no list"):
        census(root, (("ONE", ("a.py",)),))


def test_a_list_naming_a_deleted_declaration_fails_the_build(
        tmp_path: pathlib.Path) -> None:
    """The other direction, and a different mistake.

    A file nobody listed emits nothing. A name with no file behind it
    is a reader opening a path that is not there, which fails later
    and says less. Both are the lists and the directory disagreeing,
    and a message that said only that would not say what to do."""
    from huggorm_decl import census

    root = _corpus_dir(tmp_path)
    with pytest.raises(TypeError, match="listed and not on disk"):
        census(root, (("ONE", ("a.py", "b.py", "gone.py")),))


def test_a_declaration_in_two_lists_fails_the_build(
        tmp_path: pathlib.Path) -> None:
    """One document, one group.

    The groups say how the BUILD treats a declaration - a compiled
    module, a plain-Python vocabulary, the error hierarchy, or
    nothing at all. A file in two of them is two answers to one
    question, and the census counts every name once, so without this
    a duplicate would make the totals agree by accident."""
    from huggorm_decl import census

    root = _corpus_dir(tmp_path)
    with pytest.raises(TypeError, match="in both ONE and TWO"):
        census(root, (("ONE", ("a.py", "b.py")), ("TWO", ("b.py",))))


def test_the_real_declaration_set_agrees_with_its_directory() -> None:
    """The corpus this build reads passes its own census.

    Through the same call the build makes, so a disagreement in
    `decl/` fails here as well as there. It says nothing about
    whether that call is still in `corpus()`; the test below is what
    says that."""
    import huggorm_decl

    huggorm_decl.corpus()


def test_the_census_runs_when_the_corpus_is_built(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """`corpus()` calls the census, and this notices when it stops.

    The three tests above drive a temporary directory. They prove the
    function and say nothing about the one line that reaches it, so
    deleting that line would leave every one of them passing - which
    is the same shape as the bug this whole task is about: a thing
    that stopped happening and said nothing.

    `cache_clear` twice, and both matter. `corpus()` is
    `functools.cache`d, so without the first the census may already
    have run in an earlier test and this would call nothing; without
    the second, every later test gets a corpus built while the stub
    was in place."""
    import huggorm_decl

    ran = []
    monkeypatch.setattr(huggorm_decl, "census",
                        lambda *args: ran.append(args))
    huggorm_decl.corpus.cache_clear()
    try:
        huggorm_decl.corpus()
    finally:
        huggorm_decl.corpus.cache_clear()
    assert ran, "corpus() no longer runs the census"
