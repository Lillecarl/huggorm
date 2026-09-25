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
import dataclasses
import pathlib
from collections.abc import Callable
from typing import Any

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


# One bound class that declares a dunder. `__call__` because that is
# the one a declaration actually wants - `await f.apply(x)` is what
# `tasks/034` shipped, and `await f(x)` is what it could not say.
CALLABLE = '''"""One bound class that declares __call__."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/expr/value.hh")
@binding(cxx="nix::Value", threading="affine", blocking=True)
class Callable:
    """A value, for a test that never compiles one."""

    def __call__(self, arg: Str) -> Str:
        """Apply this to one argument."""
        Cxx("return arg;")
'''


def test_a_declared_dunder_is_refused_rather_than_dropped(
        tmp_path: pathlib.Path) -> None:
    """A declaration that writes `__call__` gets an answer.

    It used to get NOTHING. The class-body loop kept the names that
    are not `__`-prefixed and skipped the rest, so a declared dunder
    reached no binding, no stub line and no manifest entry, with no
    diagnostic anywhere - and a skip is indistinguishable from an
    absence, which is this repo's named failure mode (tasks/088).

    Measured before the refusal, on a probe declaring `__call__` and
    `__len__` beside one plain accessor:

        Probe ['nar_size'] ctor= None

    Two declared methods gone, and the read reported success.

    The message points at `tasks/088` rather than describing what a
    dunder would take, because the answer for a caller today is to
    declare a plain name. `Value.apply` is that name."""
    from huggorm_dsl.read import DeclarationError, read

    path = _declaration(tmp_path, CALLABLE)
    with pytest.raises(DeclarationError, match="tasks/088") as caught:
        read(path)
    # The LINE, because a refusal whose answer is "rename this" has to
    # say which one. `__call__` is the eleventh line of the fixture.
    assert ":11:" in str(caught.value)
    assert "Callable.__call__" in str(caught.value)


# One declaration with an `async def` in each of the three places it
# can sit: a method, a decorated free function, and an undecorated
# helper. The answers differ, which is the point (tasks/088).
#
# ONE async at a time, and the three tests below each turn on the one
# they are about. Written with all three async first, and that gate
# was weak: the class test's `match="async def"` was satisfied by the
# FREE function's refusal, so it passed with the class refusal
# removed and failed on a later assertion instead.
ASYNCS = '''"""One declaration written with async defs."""

from huggorm_dsl.declare import Cxx, Str, binding, header, threading


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("return std::string();")


@threading("pool")
def free_one(text: Str) -> Str:
    """Decorated, so it was meant to be a binding."""
    Cxx("return text;")


async def helper() -> None:
    """Undecorated, so the declaration wrote it for itself."""
'''

# The one edit each test makes, so no fixture carries two asyncs.
AS_METHOD = ("    def base16", "    async def base16")
AS_FREE = ("\ndef free_one", "\nasync def free_one")


def test_an_async_def_says_why_it_is_refused(
        tmp_path: pathlib.Path) -> None:
    """`async def` in a declaration names its own cause.

    It was refused before this, by accident. The import keeps the
    function, so it named a live line; `ast.AsyncFunctionDef` is not
    a subclass of `ast.FunctionDef`, so `_reconcile`'s node set had
    no line there; and the message a reader got was

        the import kept definitions at lines [16] and the tree has no
        node there. The two readings have stopped lining up - most
        likely `co_firstlineno` no longer points where this assumes.

    Loud, and blaming the wrong thing. Nothing in it says "async",
    and the cause it names - a Python release moving
    `co_firstlineno` - sends a reader to the wrong file entirely.

    The refusal is at the loop that drops it now, so it can say what
    a declaration is: a description of a C++ binding, where the async
    form is DERIVED from `@binding(threading=...)` and `@blocks`."""
    from huggorm_dsl.read import DeclarationError, read

    source = ASYNCS.replace(*AS_METHOD)
    with pytest.raises(DeclarationError, match="async def") as caught:
        read(_declaration(tmp_path, source))
    assert "Digest.base16" in str(caught.value)
    assert "co_firstlineno" not in str(caught.value)


def test_a_free_async_function_is_refused_too(
        tmp_path: pathlib.Path) -> None:
    """The module-level loop drops one the same way.

    A second loop, so a second refusal - and the class one cannot
    stand in for it, because a declaration may have free functions
    and no class at all."""
    from huggorm_dsl.read import DeclarationError, read

    source = ASYNCS.replace(*AS_FREE)
    with pytest.raises(DeclarationError, match="async def") as caught:
        read(_declaration(tmp_path, source))
    assert "free_one" in str(caught.value)


def test_an_undecorated_async_helper_is_left_alone(
        tmp_path: pathlib.Path) -> None:
    """The reconcile half, and the half that is not a refusal.

    An UNDECORATED def at module level is a helper the declaration
    wrote for itself - the free-function loop has always skipped one,
    and this asserts an `async def` helper is skipped the same way.

    It could not be written at all before. `_reconcile` ran first and
    raised on it, because the import kept it and the tree reader had
    no node at its line, so an async helper failed the whole read.
    Adding `ast.AsyncFunctionDef` to `DEFINITIONS` is what fixed
    that.

    Dropping it again fails all THREE of these, which is more than
    this gate was written expecting - the docstring here claimed the
    two refusals would still pass. They do not, and the reason is the
    order: `_reconcile` runs before anything reads a class body, so
    without `DEFINITIONS` the refusals are UNREACHABLE and the two
    tests above fail on the message rather than on the raise.

        FAILED test_an_async_def_says_why_it_is_refused
          AssertionError: Regex pattern did not match.
        FAILED test_a_free_async_function_is_refused_too
          AssertionError: Regex pattern did not match.
        FAILED test_an_undecorated_async_helper_is_left_alone
          DeclarationError: ... the import kept definitions at lines
        3 failed, 361 passed, 10 deselected"""
    from huggorm_dsl.read import read

    # ASYNCS as written: only the helper is async.
    mod = read(_declaration(tmp_path, ASYNCS))
    assert [c.name for c in mod.classes] == ["Digest"]
    assert [f.name for f in mod.functions] == ["free_one"]


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


# A marker decorator over a `@property`. The marker sets an attribute
# on what it is handed and a property object takes none, so the
# module raises while Python is still executing it.
UNIMPORTABLE = '''"""One accessor with a marker over a @property."""

from huggorm_dsl.declare import Cxx, Str, binding, header, instant


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    @instant
    @property
    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("return self.to_string();")
'''

# One accessor written as a @staticmethod, which no emitter has a
# word for. Its parameter is the point: a bound method is read by
# skipping the first one.
STATIC = '''"""One accessor written as a @staticmethod."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    @staticmethod
    def of(text: Str) -> Str:
        """The digest of some text."""
        Cxx("return nix::hashString(text);")
'''


def test_a_declaration_that_will_not_import_is_refused(
        tmp_path: pathlib.Path) -> None:
    """The import error reaches a reader, with Python's own reason.

    `load` used to answer None here and the reader fell back to the
    tree alone. That reads as a working declaration for every file
    with no `NIX_VERSION` branch in it, which is all of them: the
    same nodes survive either way, so nothing said a word - and
    nothing said it about the files importing from it either
    (tasks/082).

    The cause is carried because it is one line to fix and impossible
    to guess at. `@instant` over `@property` sets an attribute on a
    descriptor, and "it did not import" alone would send a reader to
    the wrong file."""
    from huggorm_dsl.read import DeclarationError, read

    path = _declaration(tmp_path, UNIMPORTABLE)
    with pytest.raises(DeclarationError, match="does not import"):
        read(path)
    with pytest.raises(DeclarationError, match="'property' object"):
        read(path)


# The SAME accessor with the decorators the other way round. The
# marker reaches the function, the property wraps what it returns,
# and the module imports.
IMPORTABLE = UNIMPORTABLE.replace("    @instant\n    @property",
                                  "    @property\n    @instant")

# A file that will not import for a reason that has nothing to do
# with a descriptor. The control: the hint must not be appended to
# every import failure, or it says nothing.
UNRELATED = '''"""One accessor naming something that does not exist."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    NOT_A_NAME

    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("return self.to_string();")
'''


def test_a_refusal_names_the_file_whatever_type_the_path_was(
        tmp_path: pathlib.Path) -> None:
    """The position is built by concatenation, so the stack holds str.

    `DeclarationError` reads `_READING[-1]` and does `where +=
    f":{line}"`. `_READING` is annotated `list[str]` and nothing
    enforced it, so a `pathlib.Path` pushed onto it raised

        TypeError: unsupported operand type(s) for +=: 'PosixPath'
        and 'str'

    INSIDE the refusal - losing the message it was about to give,
    which is the worst place to fail. Every other function in the
    reader tolerates a Path: `load` does `pathlib.Path(path).stem`.

    Normalised at the one WRITE to the stack rather than at every
    read of it. `read` and `resolved` push through `reading` now
    instead of each hand-rolling the same append/try/finally/pop, so
    there is one place to normalise.

    Perturbation: drop the `str()` in `reading` and this fails with
    the TypeError above."""
    import ast

    from huggorm_dsl.read import DeclarationError, reading

    node = ast.parse("x = 1").body[0]
    with reading(tmp_path / "decl.py"), \
            pytest.raises(DeclarationError) as caught:
        raise DeclarationError(node, "refused")

    assert "decl.py:1:1: refused" in str(caught.value), str(caught.value)

    # The str path is unchanged, which is what says the fix widened
    # rather than moved.
    with reading(str(tmp_path / "decl.py")), \
            pytest.raises(DeclarationError) as same:
        raise DeclarationError(node, "refused")
    assert str(same.value) == str(caught.value)


def test_a_marker_over_a_descriptor_says_which_order_to_write(
        tmp_path: pathlib.Path) -> None:
    """Python's message says WHAT broke. This says what to do.

    `AttributeError: 'property' object has no attribute '_instant'`
    is accurate and is not actionable: it names the descriptor and
    the attribute and stops, so a reader has to work out on their own
    that the two decorators can simply be swapped.

    `tasks/076` asked for the answer to be in a REFUSAL rather than
    in a comment, and this is it. The answer was MEASURED both ways
    rather than reasoned about - see the second assertion, which is
    the one that says the advice is true.

    Derived from the message shape, not from a list of markers: every
    marker in `declare.py` writes `_<name>` onto what it is handed,
    so a list would be that fact stated twice and would go stale on
    the next marker.

    It is honest about what the swap buys, and that is deliberate.
    The order fixes the IMPORT and nothing else - an emitter still
    refuses a `@property` accessor - so a hint that stopped at the
    order would send a reader to a second refusal with no warning
    that one was coming."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError) as caught:
        read(_declaration(tmp_path, UNIMPORTABLE))
    said = str(caught.value)
    assert "@property OUTERMOST" in said, said
    assert "@instant" in said, "it names the marker it read, not a list"
    assert "tasks/076" in said, "and where the path ends"

    # THE ADVICE IS TRUE, measured rather than asserted. Written the
    # way the refusal says, the file imports AND both facts survive:
    # `_apply` skips a builtin decorator and applies the marker to
    # its throwaway, so the marker reaches it from either position,
    # and `prop` is read from the tree either way.
    good = tmp_path / "good"
    good.mkdir()
    method = read(_declaration(good, IMPORTABLE)).classes[0].methods[0]
    assert method.prop, "the tree still says it is an attribute"
    assert method.instant, "and the marker was not lost on the way"

    # THE CONTROL. An import failure with no descriptor in it gets no
    # hint - without this the gate passes for a hint appended to
    # everything, which would be advice on a file it does not fit.
    other = tmp_path / "other"
    other.mkdir()
    with pytest.raises(DeclarationError) as unrelated:
        read(_declaration(other, UNRELATED))
    assert "does not import" in str(unrelated.value)
    assert "OUTERMOST" not in str(unrelated.value), str(unrelated.value)

    # `property` IS THE ONLY ONE, and the first version of the hint
    # named `staticmethod` and `classmethod` beside it. Measured on
    # 3.14.7: only a property refuses an attribute, so a marker over
    # a `@staticmethod` IMPORTS and reaches `_method`'s own refusal -
    # which already names the real problem. Those two arms were text
    # that could never run (tasks/075), and this is what says so.
    static = tmp_path / "static"
    static.mkdir()
    marked = STATIC.replace("    @staticmethod",
                            "    @instant\n    @staticmethod")
    marked = marked.replace("binding, header",
                            "binding, header, instant")
    with pytest.raises(DeclarationError) as over_static:
        read(_declaration(static, marked))
    assert "@staticmethod" in str(over_static.value)
    assert "does not import" not in str(over_static.value), \
        "a staticmethod takes the attribute, so the import survives"


def test_an_accessor_declared_static_is_refused(
        tmp_path: pathlib.Path) -> None:
    """`@staticmethod` says the first parameter is not self, and the
    reader reads a bound method by skipping the first parameter.

    Measured with the refusal removed: `of(text)` read as `of()`. The
    parameter was gone, and every emitter would then have written a
    signature short of an argument - the shape of the `open_store(uri)`
    bug `_method`'s own docstring records.

    Reachable only since `_live` learnt to look through a descriptor.
    Before that a `@staticmethod` carried no `__code__`, named no live
    line, and was dropped whole in silence (tasks/075). The behaviour
    changed there and no gate held it; this is that gate (tasks/076)."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError, match="@staticmethod"):
        read(_declaration(tmp_path, STATIC))

    # A second directory, because `load` caches by path and `read`
    # would answer from the first file otherwise.
    second = tmp_path / "second"
    second.mkdir()
    classmethod_too = STATIC.replace("@staticmethod", "@classmethod")
    with pytest.raises(DeclarationError, match="@classmethod"):
        read(_declaration(second, classmethod_too))


# A bound class with one accessor behind a version branch. The corpus
# has no branch at all, so the one exemption the read census makes is
# written here or it is never read (tasks/081).
BRANCHED_ACCESSOR = '''"""One bound class whose accessor is behind a version."""

from huggorm_dsl.declare import NIX_VERSION, Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("return self.to_string();")

    if NIX_VERSION >= (2, 0):

        def here(self) -> Str:
            """The arm this build has."""
            Cxx("return self.here();")

    else:

        def gone(self) -> Str:
            """The arm it does not."""
            Cxx("return self.gone();")
'''


def _one_file_corpus(tmp_path: pathlib.Path, source: str) -> Any:
    """A `Corpus` over one declaration, the way `nbcheck` builds one.

    The real set is `huggorm_decl.corpus()`, and it is cached and
    global. A test that wants a declaration the corpus does not have
    builds its own, which the docstring on `corpus()` says is the
    supported way."""
    from huggorm_dsl.corpus import Corpus

    (tmp_path / "digest.py").write_text(source)
    return Corpus(tmp_path, nanobind=("digest.py",))


def test_a_branch_arm_that_lost_is_not_a_missing_definition(
        tmp_path: pathlib.Path) -> None:
    """The one definition a declaration may write that reaches nothing.

    Python resolves `NIX_VERSION` during the import, so one arm of an
    `if` survives and the other is MEANT to vanish. The census reads
    the RAW parse, where both arms are still there, so without this
    exemption every branched declaration would fail the build.

    Written here because the corpus has no branch: `read.py` imports
    every declaration for exactly this reason and no declaration in
    this repo uses it, which is how the errors emitter came to see
    neither arm and say nothing (tasks/073)."""
    from huggorm_gen.cppgen import generate

    have = _one_file_corpus(tmp_path, BRANCHED_ACCESSOR)
    generate.census_read(have)

    mod = have.module("digest.py")
    kept = {m.name for m in mod.classes[0].methods}
    assert kept == {"base16", "here"}, "the import chose an arm"


def test_the_census_notices_a_definition_the_reader_dropped(
        tmp_path: pathlib.Path) -> None:
    """A method in the file and not in the reader's output fails.

    This is `tasks/075` in one assertion. A `@property` accessor named
    no live line, `_resolve` dropped its node, and it vanished from
    the binding, from `_parts`, from `__repr__`, from `__hash__` and
    from `_wire_fields` - with the only complaint coming from a
    hand-written `_from_parts` that still named it. A class whose
    `_from_parts` is derived would have lost the field in silence.

    Dropped by hand here rather than by reproducing the reader bug:
    what the census is asked is "did anything keep this", and a
    method removed from `methods` is that question's failing input
    whatever removed it. The reader bug itself was reproduced against
    the real corpus - see the task."""
    from huggorm_dsl.read import Class
    from huggorm_gen.cppgen import generate

    have = _one_file_corpus(tmp_path, BRANCHED_ACCESSOR)
    full = have.module("digest.py")
    lost = dataclasses.replace(
        full.classes[0], methods=tuple(m for m in full.classes[0].methods
                                       if m.name != "base16"))
    assert isinstance(lost, Class)

    class Dropped:
        nanobind = ("digest.py",)
        vocabularies = ()
        # No errors declaration. This corpus is one bound class, and
        # the errors half of the census asks a different question of a
        # different file.
        errors = ""

        def path(self, name: str) -> Any:
            return have.path(name)

        def module(self, name: str) -> Any:
            return dataclasses.replace(full, classes=(lost,))

    with pytest.raises(TypeError, match="base16"):
        generate.census_read(Dropped())


def test_the_codegen_runs_the_read_census(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The census is called, and this notices when it stops being.

    The two tests above drive the function. They say nothing about the
    one line that reaches it, so deleting that line would leave both
    passing - which is the same shape as the bug the census exists to
    catch: something that stopped happening and said nothing.

    The same argument as `test_the_census_runs_when_the_corpus_is_built`
    above, for the other census (tasks/078)."""
    from huggorm_gen.cppgen import generate

    ran = []
    monkeypatch.setattr(generate, "census_read", lambda have: ran.append(have))
    assert generate.main(str(tmp_path)) == 0
    assert ran, "the codegen no longer runs the read census"


def test_the_emitter_must_write_every_method_the_reader_kept() -> None:
    """The second seam: what the reader kept, and what the emitter wrote.

    Checked against the TEXT the emitter produced, not against a
    second walk of the same `Class` objects - that would be two views
    of one decision agreeing with itself, which is the blind spot
    `census_read` exists to avoid on the other side.

    `pathinfo.py` because it is the class 075 lost an accessor from,
    and `nar_size` because it is a plain one: no marker, no
    exemption, nothing else to explain its absence."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import generate
    from huggorm_gen.cppgen.nbemit import bindable, extension

    have = corpus()
    mod = have.module("pathinfo.py")
    bound = bindable(mod)
    text = extension(mod, "huggorm_bindings.pathinfo", chain=[], errors="")
    generate.census_written(mod, bound, text)

    lost = text.replace('def("nar_size"', 'def("not_that_one"')
    assert lost != text, "the emitter no longer writes nar_size at all"
    with pytest.raises(TypeError, match="nar_size"):
        generate.census_written(mod, bound, lost)


def test_a_function_the_emitter_writes_another_way_is_not_missing() -> None:
    """Three declared functions that are not bound names, and none of
    them is exempt by name.

    `store.py` has all three shapes. `_init_libstore` is `@startup`,
    emitted as a CALL at module init; `_translate_nix_error` is a
    `@translator`, emitted as a registration; and `open_store` is what
    `Store` names in `@produced(by=...)`, emitted as that class's
    `nb::new_` - a caller writes `Store(uri)` and never the function.

    Without the third the census would fail the real corpus, which is
    how it was found."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import generate
    from huggorm_gen.cppgen.nbemit import bindable, extension

    have = corpus()
    mod = have.module("store.py")
    declared = {f.name for f in mod.functions}
    assert {"open_store", "_init_libstore", "_translate_nix_error"} <= declared

    text = extension(mod, "huggorm_bindings.store", chain=[], errors="")
    assert 'def("open_store"' not in text, "it is a constructor, not a name"
    generate.census_written(mod, bindable(mod), text)


def test_the_codegen_runs_the_written_census(
        monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> None:
    """The census is called for every module the codegen emits.

    Same argument as the two wiring tests above: the tests that drive
    the function say nothing about the line that reaches it."""
    from huggorm_gen.cppgen import generate

    ran = []
    monkeypatch.setattr(generate, "census_written",
                        lambda mod, bound, text: ran.append(mod.name))
    assert generate.main(str(tmp_path)) == 0
    assert "pathinfo" in ran, ran


def test_a_catch_brings_the_header_that_declares_it() -> None:
    """The other half of the same rule, and the same measurement.

    The emitted translator catches `nix::InvalidPath`,
    `nix::BadStorePathName` and their kind. Until 2026-09-05 the three
    headers that declare them sat in `cpp/errors.hpp`, which every
    emitted file includes - a fact about generated code, written into
    a file a person maintains (`tasks/090`).

    `decl/errors.py` now says `header = "nix/..."` beside each `cxx`,
    and the emitter writes the include beside the chain.

    NOT load-bearing, measured before this was written: removing all
    three from the header still compiled, because `path.cpp` reaches
    `nix::InvalidPath` through `nix/store/path.hh`'s own transitive
    include. So the compiler cannot gate this either, and the emitted
    TEXT is what can.

    A file with NO translator is the control. It catches nothing, so
    it asks for none of them."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import pyerrors
    from huggorm_gen.cppgen.nbemit import extension

    have = corpus()
    headers = pyerrors.headers(have.resolved(have.errors))
    assert headers == ["nix/store/store-api.hh",
                       "nix/store/store-dir-config.hh",
                       "nix/util/error.hh",
                       "nix/util/signals.hh"], headers

    mod = have.module("path.py")
    assert mod.translators, "the control below means nothing otherwise"
    text = extension(mod, "huggorm_bindings.path", chain=[],
                     errors="", error_headers=headers)
    for h in headers:
        assert f'#include "{h}"' in text, h

    without = extension(mod, "huggorm_bindings.path", chain=[], errors="")
    assert '#include "nix/store/store-dir-config.hh"' not in without, \
        "the headers come from the derivation, not from a fixed list"


def test_a_caught_error_must_say_which_header_declares_it() -> None:
    """Both directions refused, because either line alone reaches
    nothing.

    A `cxx` with no `header` is a catch whose type the emitted file
    can only reach by accident. A `header` with no `cxx` is a line no
    emitter reads, and this repo's named failure mode is a line
    nobody reads looking exactly like one nobody wrote.

    Drop either refusal and the matching half of this passes."""
    import ast

    from huggorm_dsl.read import DeclarationError
    from huggorm_gen.cppgen import pyerrors

    no_header = ast.parse(
        'class NixError(Exception):\n'
        '    cxx = "nix::Error"\n')
    with pytest.raises(DeclarationError, match="which header declares it"):
        pyerrors.headers(no_header)

    no_cxx = ast.parse(
        'class NixError(Exception):\n'
        '    header = "nix/util/error.hh"\n')
    with pytest.raises(DeclarationError, match="`header` with no `cxx`"):
        pyerrors.headers(no_cxx)


def test_the_header_line_does_not_reach_the_emitted_module() -> None:
    """`header` is C++, so it goes where `cxx` goes: nowhere a caller
    can see.

    A caller catches `huggorm_bindings.errors.InvalidPath` and can do
    nothing with the name of a nix header. Drop `HEADER` from
    `module`'s filter and this fails."""
    import ast

    from huggorm_decl import corpus
    from huggorm_gen.cppgen import pyerrors

    have = corpus()
    tree = have.resolved(have.errors)
    text = pyerrors.module(tree, "doc")
    lines = [ln.strip() for ln in text.splitlines()]
    assert not [ln for ln in lines if ln.startswith("header =")], text
    assert not [ln for ln in lines if ln.startswith("cxx =")], text
    # The classes still arrive, so the absence above is a strip and
    # not an empty module. Prose may still say "cxx" - a docstring
    # explaining the declaration is not a line a caller can act on -
    # so the assertions above look at ASSIGNMENTS.
    assert "class InvalidPath" in text
    ast.parse(text)


def test_emitting_the_module_leaves_the_declaration_alone() -> None:
    """The tree is SHARED, so a transform that mutates it poisons the
    next reader.

    `corpus()` is cached for the process and hands every emitter the
    same tree. `module` stripped `cxx` and `header` in place, so any
    reader after it saw an exception declaration with no C++ in it:
    `chain` would emit a translator catching NOTHING and `headers` an
    empty include block. Both compile, and both are silent - this
    repo's named failure mode.

    It went unnoticed because `generate.py` happens to call
    `error_chain` BEFORE `module`. Reversing those two lines is the
    perturbation, and it needs no test to be a bug.

    Found 2026-09-05, by `headers` reading [] where the same call had
    read three headers a moment earlier."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import pyerrors

    have = corpus()
    tree = have.resolved(have.errors)

    before = pyerrors.chain(tree, "raise_as", "pkg.errors")
    pyerrors.module(tree, "doc")
    after = pyerrors.chain(tree, "raise_as", "pkg.errors")

    assert before == after, "the transform kept its hands off the tree"
    assert "nix::InvalidPath" in "\n".join(after)
    assert pyerrors.headers(tree), "and the headers survive it too"


def test_a_body_brings_its_own_standard_header() -> None:
    """What a `Cxx` body SPELLS decides what the emitted file
    includes.

    `eval.py`'s bodies throw `std::invalid_argument` 54 times, and
    the emitted `eval.cpp` includes `<stdexcept>` because of that -
    not because a hand-written header carries one on its behalf.
    That was the arrangement `tasks/090` found: `eval.hpp` held a
    `<stdexcept>` it never used, a fact about generated code living
    in a file a person maintains.

    NOT load-bearing, and that is measured rather than hoped. With
    both the header's include AND this derivation removed, the build
    still compiles - nix's own headers reach `<stdexcept>` somewhere
    along the chain. So the compiler cannot gate this, and asserting
    on the emitted TEXT is what can: the question is whether the
    emitter derives the include, not whether the build tolerates its
    absence.

    `pathinfo.py` is the control. Its bodies spell `std::uint64_t`
    and `std::move` and throw nothing, so it gets `<cstdint>` and
    `<utility>` and NOT `<stdexcept>` - which is what says the
    derivation reads the body rather than adding a fixed list."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen.nbemit import extension

    have = corpus()
    text = extension(have.module("eval.py"), "huggorm_bindings.eval",
                     chain=[], errors="")
    assert "#include <stdexcept>" in text
    assert "std::invalid_argument" in text, "the body that needs it"

    other = extension(have.module("pathinfo.py"),
                      "huggorm_bindings.pathinfo", chain=[], errors="")
    assert "#include <cstdint>" in other
    assert "#include <stdexcept>" not in other, \
        "nothing in pathinfo throws, so nothing asks for it"
