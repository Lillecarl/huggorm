"""
Gates over the DECLARATION reader and the emitters that read a tree.

Nothing here builds or imports a binding. What it drives is the half
of the build that turns a declaration into a document, on declarations
written for the test - so it can state a case the real corpus does not
have, and does not have to invent one there.

A version branch is that case. `read.py` imports every declaration so
Python resolves `if NIX_VERSION >= ...`, and the errors emitter parsed
the file a second time and saw neither arm (huggorm#73). No declaration
in this repo branches today, so nothing real can hold the fix.
"""

import ast
import pathlib
import re
from typing import Any

import pytest

BRANCHED = '''"""Two exception classes, one behind a version."""

from huggorm_dsl.declare import NIX_VERSION


class NixError(Exception):
    """The base every other one derives from."""

    cxx = "nix::Error"
    header = "nix/util/error.hh"


if NIX_VERSION >= (2, 0):

    class Here(NixError):
        """The arm this build has - the version is long past."""

        cxx = "nix::Here"
        header = "nix/util/error.hh"

else:

    class Gone(NixError):
        """The arm it does not have."""

        cxx = "nix::Gone"
        header = "nix/util/error.hh"
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


def _errors(mod: Any, ir: Any) -> Any:
    """The errors model of one declaration read on its own."""
    return ir.Errors.of("pkg.errors", mod.errors, ir.Resolver.of(mod))


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

    Three outputs come off an exception declaration - the model's
    error entries, the translator's catch chain, and the emitted
    module - and all three must agree on what a branch means. A
    reader of the top level misses a branched class, and a copy of
    the whole document emits both the class and the `if` around it.

    So `Gone` must be absent from all three and `Here` present in all
    three. Asserting only one direction would pass on an emitter that
    kept everything."""
    from huggorm_dsl.read import read, resolved
    from huggorm_gen import ir
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    tree = resolved(path)

    errors = _errors(read(path), ir)
    assert sorted(errors.classes) == ["Here", "NixError"]
    # ...and it inherits, which is the half only the IMPORT knows.
    assert errors.classes["Here"].bases == ("NixError",)

    chain = "\n".join(pyerrors.chain(errors, "raise_as"))
    assert "nix::Here" in chain
    assert "nix::Gone" not in chain
    # Most-derived first, or the base swallows the subclass.
    assert chain.index("nix::Here") < chain.index("nix::Error")

    emitted = pyerrors.module(tree, "emitted")
    assert "class Here" in emitted
    assert "Gone" not in emitted


READERLESS = '''
from huggorm_dsl.declare import I64


class NixError(Exception):
    cxx = "nix::Error"
    header = "nix/util/error.hh"
    _wire_fields = (("message", str), ("colored", str), ("code", I64))


class Read(NixError):
    cxx = "nix::Read"
    header = "nix/util/error.hh"
    reader = "huggorm::error_info"
'''


def test_a_part_beyond_the_message_needs_a_reader(
        tmp_path: pathlib.Path) -> None:
    """A catch with a part and no reader calls the constructor short,
    and `raise_as` turns that refusal into a RuntimeError in silence.

    So the reader refuses it, and the shipped declaration, whose
    classes name a reader, gets one argument per extra part, typed by
    the part's record."""
    from huggorm_dsl.read import DeclarationError, read
    from huggorm_gen.cppgen import pyerrors
    from huggorm_gen.cppgen.generate import declared_model

    path = _declaration(tmp_path, READERLESS)
    with pytest.raises(DeclarationError, match="NixError: `_wire_fields`"):
        read(path)

    chain = "\n".join(pyerrors.chain(declared_model().errors, "raise_as"))
    assert ('"ThrownError", e, '
            "huggorm::error_info<huggorm::ErrorInfo>);") in chain


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
    one cannot - a surviving `ast.If`."""
    from huggorm_dsl.read import DeclarationError
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    raw = ast.parse(pathlib.Path(path).read_text())
    with pytest.raises(DeclarationError, match="version branch"):
        pyerrors.module(raw, "emitted")


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
# it was written until it was deleted (huggorm#75).
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


OVERLOADED = '''"""One bound class with an overloaded method."""

from typing import overload

from huggorm_dsl.declare import Cxx, I64, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    @overload
    def part(self, at: I64) -> Str:
        """By position."""
        Cxx("return self.at(at);")

    @overload
    def part(self, at: Str) -> Str:
        """By name."""
        Cxx("return self.named(at);")

    def part(self, at: Str) -> Str:
        """Either."""
'''


OVERLOADED_FREE = '''"""One free function, overloaded through the module."""

import typing

from huggorm_dsl.declare import I64, Str, binds


@typing.overload
@binds("huggorm::part")
def part(at: I64) -> Str:
    """By position."""


@binds("huggorm::part")
def part(at: Str) -> Str:
    """Either."""
'''


@pytest.mark.parametrize(("source", "line"), [
    (OVERLOADED, 14), (OVERLOADED_FREE, 10)])
def test_an_overload_is_refused(
        tmp_path: pathlib.Path, source: str, line: int) -> None:
    """Every surface binds one definition per name, so the stub would
    hold duplicate `def`s and the RPC layer one route twice. The
    import answers, so the module spelling is refused as well."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError, match="part is overloaded") as caught:
        read(_declaration(tmp_path, source))
    assert f":{line}:" in str(caught.value)


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


# One record whose `text` crosses as bytes. `{local}` is filled in by
# the test, so the same source reads with and without the marker the
# rule depends on.
WIRE_READ = '''"""One record with a part read through another accessor."""

from huggorm_dsl.declare import (
    I64, Bytes, Str, binding, local, produced, reads, wire_read, wire_value)


@produced
@binding(threading="pool", blocking=False)
@wire_value()
class Line:
    """A line, for a test that never compiles one."""

    @wire_read("text_bytes")
    def text(self) -> Str:
        """The text."""

    def number(self) -> I64:
        """The line number."""

    {local}
    @reads("text")
    def text_bytes(self) -> Bytes:
        """The bytes of `text`."""
'''


def test_a_part_read_through_another_accessor_is_still_derived(
        tmp_path: pathlib.Path) -> None:
    """`@wire_read` changes one part's reader, and nothing else.

    The parts stay derived from the accessors, so an accessor added
    later crosses with no list to update. A `wire_value(fields=...)`
    list did the same job and left a new accessor off the wire,
    silently.

    The reader must be `@local`: otherwise it would cross a second
    time as a part of its own. Without the marker, the read refuses."""
    from huggorm_dsl.read import read

    path = _declaration(tmp_path, WIRE_READ.replace("{local}", "@local"))
    cls = read(path).classes[0]
    assert [(f.name, f.read) for f, _ in cls.parts] == [
        ("text", "text_bytes"), ("number", "number")]

    (tmp_path / "unmarked").mkdir()
    path = _declaration(tmp_path / "unmarked",
                        WIRE_READ.replace("    {local}\n", ""))
    with pytest.raises(TypeError, match="must be a @local accessor"):
        _ = read(path).classes[0].parts


def test_a_produced_class_nothing_returns_fails_the_build(
        tmp_path: pathlib.Path) -> None:
    """A produced class is made by a call that returns it, and the
    build reads those calls off the return types. `Line` is produced
    and nothing here returns one, so it has no way in at all. The real
    set is the control: every produced class in it has a call."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir
    from huggorm_gen.cppgen.generate import declared_model

    mod = read(_declaration(tmp_path, WIRE_READ.replace("{local}", "@local")))
    unit = ir.ModuleModel.of(mod, "")
    with pytest.raises(TypeError, match=r"^Line: declared @produced"):
        ir.Model({c.name: c for c in unit.classes}, {}, {},
                 {}, ir.Errors("", {}), (unit,))

    model = declared_model()
    assert model.producers["PathInfo"] == ("Store.query_path_info",)
    # A dict's value is made too: on 2.35 this is the only call that
    # makes an UnkeyedRealisation (huggorm#129).
    built = next(m for m in model.classes["BuildSuccess"].bound
                 if m.name == "built_outputs")
    returns = built.returns
    assert returns is not None and returns.origin is ir.Origin.DICT
    assert "BuildSuccess.built_outputs" in model.producers[returns.leaf.name]


# One bound class that declares a dunder the emitters carry and one
# they do not.
CALLABLE = '''"""One bound class that declares two dunders."""

from huggorm_dsl.declare import Cxx, I64, Str, binding, header


@header("nix/expr/value.hh")
@binding(cxx="nix::Value", threading="affine", blocking=True)
class Callable:
    """A value, for a test that never compiles one."""

    def __call__(self, arg: Str) -> Str:
        """Apply this to one argument."""
        Cxx("return arg;")

    def __len__(self) -> I64:
        """No emitter carries this one."""
        Cxx("return 0;")
'''


def test_an_undeclarable_dunder_is_refused_rather_than_dropped(
        tmp_path: pathlib.Path) -> None:
    """A dunder the emitters are not taught gets an answer.

    Without the refusal, the class-body loop would keep the names
    that are not `__`-prefixed and skip the rest, so a declared dunder
    would reach no binding, no stub line and no rpc, with no
    diagnostic anywhere - and a skip is indistinguishable from an
    absence, which is this repo's named failure mode (huggorm#88).

    `__call__` IS taught, so the refusal names `__len__` alone, at
    its line."""
    from huggorm_dsl.read import DeclarationError, read

    path = _declaration(tmp_path, CALLABLE)
    with pytest.raises(DeclarationError, match="huggorm#88") as caught:
        read(path)
    assert ":15:" in str(caught.value)
    assert "Callable.__len__" in str(caught.value)
    assert "Callable.__call__" not in str(caught.value)


DERIVED = '''"""One bound class that names a base."""

from huggorm_dsl.declare import binding, header


@header("nix/store/store-api.hh")
@binding(cxx="nix::Store", threading="pool")
class Base:
    """A base."""


@header("nix/store/local-store.hh")
@binding(cxx="nix::LocalStore", threading="pool")
class Leaf(Base):
    """No emitter carries a hierarchy, so this is refused."""
'''


def test_a_bound_base_is_refused_rather_than_dropped(
        tmp_path: pathlib.Path) -> None:
    """No emitter carries a class hierarchy, so a base would be
    dropped in silence; the reader refuses it at its line
    (huggorm#60)."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError, match="huggorm#60") as caught:
        read(_declaration(tmp_path, DERIVED))
    assert "Leaf" in str(caught.value)


DEFAULTED = '''"""One function whose default every surface writes as source."""

from huggorm_dsl.declare import Str, needs


@needs("nix/store/store-api.hh")
def probe(x: Str = DEFAULT) -> Str:
    """A probe."""
'''


@pytest.mark.parametrize(("default", "why"), [
    ("[]", "mutable default"),
    ('float("inf")', "not a literal"),
])
def test_a_default_no_surface_can_write_is_refused(
        tmp_path: pathlib.Path, default: str, why: str) -> None:
    """Every surface writes a default as source. `[]` would be one
    shared list per surface, and `inf` is a NameError where it lands."""
    from huggorm_dsl.read import DeclarationError, read

    source = DEFAULTED.replace("DEFAULT", default)
    with pytest.raises(DeclarationError, match=why):
        read(_declaration(tmp_path, source))


def test_a_literal_default_is_read(tmp_path: pathlib.Path) -> None:
    """The negative control: a literal default reads back."""
    from huggorm_dsl.read import read

    module = read(_declaration(tmp_path, DEFAULTED.replace("DEFAULT", '"x"')))
    assert module.functions[0].params[0].default == "x"


def test_a_taught_dunder_reads_as_a_method(tmp_path: pathlib.Path) -> None:
    """`__call__` is an ordinary method to every emitter."""
    from huggorm_dsl.read import read

    path = _declaration(tmp_path, CALLABLE.split("    def __len__")[0])
    names = [m.name for m in read(path).classes[0].methods]
    assert names == ["__call__"]


# One declaration with an `async def` in each of the three places it
# can sit: a method, a decorated free function, and an undecorated
# helper. The answers differ, which is the point (huggorm#88).
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


def test_an_undecorated_async_helper_reaches_nothing(
        tmp_path: pathlib.Path) -> None:
    """An UNDECORATED def at module level reaches no output, so the
    contents check refuses it for that cause.

    The cause it is refused for is the point. Without
    `ast.AsyncFunctionDef` in `DEFINITIONS`, `_reconcile` runs first
    and refuses the helper as a line the tree has no node at, and the
    two tests above fail on the message rather than on the raise:

        FAILED test_an_async_def_says_why_it_is_refused
          AssertionError: Regex pattern did not match.
        FAILED test_a_free_async_function_is_refused_too
          AssertionError: Regex pattern did not match."""
    from huggorm_dsl.read import DeclarationError, read

    # ASYNCS as written: only the helper is async.
    with pytest.raises(DeclarationError, match="reaches no output") as caught:
        read(_declaration(tmp_path, ASYNCS))
    assert "helper (function)" in str(caught.value)


# One bound class, and one stray name the reader puts nowhere.
STRAY = '''"""One bound class beside something that is not a declaration."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("return std::string();")
{inner}
{outer}'''


@pytest.mark.parametrize(("inner", "outer", "refusal"), [
    ("", "LIMIT = 3\n", "LIMIT (int) reaches no output"),
    ("", "def helper() -> None:\n    pass\n", "helper (function) reaches"),
    ("    limit = 3\n", "", "Digest.limit (int) reaches no output"),
])
def test_a_name_the_reader_puts_nowhere_is_refused(
        tmp_path: pathlib.Path, inner: str, outer: str, refusal: str) -> None:
    """The import holds every name the build has. A name the reader
    keeps nothing for reads as one nobody wrote, so it is refused
    (huggorm#123). The class attribute was dropped in silence: the
    class loop reads only `def`s."""
    from huggorm_dsl.read import DeclarationError, read

    source = STRAY.format(inner=inner, outer=outer)
    with pytest.raises(DeclarationError, match=re.escape(refusal)):
        read(_declaration(tmp_path, source))


# One value carrying both 64-bit widths. Written here because the
# corpus has each width but never both on one class, and the fact
# under test is the DIFFERENCE between them (huggorm#79).
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

# The same width inside a CONTAINER. The uint64_t is the map VALUE's,
# so a reader that took the leaf's width for the whole type would call
# the dict a uint.
HELD = '''"""One value whose field is a container of a width."""

from huggorm_dsl.declare import U64, Cxx, binding, header, wire_value


@header("nix/store/path-info.hh")
@binding(cxx="nix::Sizes", threading="pool", blocking=False)
@wire_value()
class Sizes:
    """One number per name."""

    def by_name(self) -> dict[str, U64]:
        """How many bytes each one holds."""
        Cxx("return self.by_name;")
'''

# A PROXY whose method takes the unsigned width. A proxy is reached
# through a service, so this parameter is a message field - and the
# model has one type for it, read as a Python annotation by the
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
    model's Python spelling does NOT - a caller holds an `int`
    either way, and a stub that said `uint` would name a type Python
    does not have."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, WIDTHS))
    cls = module.classes[0]
    assert [(f.name, m.ret.wire) for f, m in cls.parts
            if m.ret is not None] == [("total", "uint"), ("when", "int")]

    typed = ir.ClassModel.of(cls, "pkg", "mod", ir.Resolver.of(module))
    assert [(f.name, f.type.scalar) for f in typed.wire_fields] == [
        ("total", "uint"), ("when", "int")]
    assert [(m.name, m.return_spelling) for m in typed.methods] == [
        ("total", "int"), ("when", "int")]


def test_a_container_of_a_width_is_refused_rather_than_guessed(
        tmp_path: pathlib.Path) -> None:
    """`dict[str, U64]` has no wire spelling, and says so.

    The uint64_t on this field is what the dict HOLDS. Naming the
    dict `uint` would be wrong and calling it a
    plain `dict[str, int]` would lose the top half of every value in
    it - so it refuses, which is the only one of the three that
    cannot be silently wrong.

    No declaration writes one today. That is why it is written here:
    the branch would otherwise be unread."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, HELD))
    # Refused where the wire spelling is rendered, which is the model.
    with pytest.raises(TypeError, match="huggorm#79"):
        ir.ClassModel.of(module.classes[0], "pkg", "mod",
                         ir.Resolver.of(module))


def test_a_service_parameter_says_which_64_bit_integer_it_is(
        tmp_path: pathlib.Path) -> None:
    """A proxy method may take a `U64`, and it crosses as a uint64.

    It was refused: a parameter was one string read both as a Python
    annotation and as a wire type, so `int` could not be right for
    both. The typed model carries the width on the leaf, so the schema
    writes a uint64 field and the codec's `Wire` names `uint`, while
    the Python surface still says `int`."""
    from google.protobuf import descriptor_pb2

    from huggorm_dsl.declare import Crossing
    from huggorm_dsl.read import read
    from huggorm_gen import ir
    from huggorm_gen.pygen.grpc_schema import build_fdset

    module = read(_declaration(tmp_path, TAKES))
    cls = module.classes[0]
    assert cls.decl.wire is Crossing.PROXY, "a plain @binding is a proxy"
    typed = ir.ClassModel.of(cls, "pkg", "mod", ir.Resolver.of(module))
    limit = typed.method("fits").params[0].type
    assert (limit.spelling, limit.scalar) == ("int", "uint")

    model = ir.Model({cls.name: typed}, {}, {}, {},
                     ir.Errors("", {}))
    fds = descriptor_pb2.FileDescriptorSet()  # type: ignore[attr-defined]
    fds.ParseFromString(build_fdset(model))
    req = next(m for m in fds.file[0].message_type
               if m.name == ir.req_name("Sizes", "fits"))
    field = next(f for f in req.field if f.name == "limit")
    assert field.type == field.TYPE_UINT64


def test_the_model_refuses_an_accessor_declared_as_an_attribute(
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
    wrong (huggorm#75).

    The model refuses it, so no surface is built from it first
    (huggorm#115)."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, ATTRIBUTE))
    with pytest.raises(TypeError, match="ATTRIBUTE"):
        ir.ModuleModel.of(module, "")


TAGGED = '''"""A tagged union, and an accessor declared against it."""

from huggorm_dsl.declare import I64, binding, fills, guard, header, names, tagged


@header("nix/expr/value.hh")
@tagged("get()", "type<true>()", int="nix::nInt", list="nix::nList")
@binding(cxx="nix::Value", threading="pool", blocking=False)
class Held:
    """A union, for a test that never compiles one."""


@header("nix/expr/value.hh")
@binding(cxx="nix::Value", threading="pool", blocking=False)
class Bare:
    """The same, with no @tagged to check against."""


@header("nix/expr/eval.hh")
@binding(cxx="nix::EvalState", threading="pool", blocking=False)
class State:
    """The owner of every method under test."""

{method}
'''


@pytest.mark.parametrize(("method", "refusal"), [
    ('    @guard("int")\n    def integer(self) -> I64:\n        """."""',
     "needs @tagged"),
    ('    @names\n    def kind(self) -> str:\n        """."""',
     "needs @tagged"),
    ('    @fills("make", "list")\n    def append(self) -> None:\n'
     '        """."""', "@fills needs a target"),
    ('    @fills("make", "list")\n    def append(self, to: Bare) -> None:\n'
     '        """."""', "carry @tagged"),
    ('    @fills("make", "set")\n    def append(self, to: Held) -> None:\n'
     '        """."""', "names no arm"),
])
def test_the_model_refuses_an_arm_it_cannot_check(
        tmp_path: pathlib.Path, method: str, refusal: str) -> None:
    """A union accessor whose arm test cannot be written is refused by
    the MODEL, not by the C++ emitter.

    Refused only at emission, it reached the stubs and the Python
    surfaces first, built from a model the binding later rejected
    (huggorm#115)."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, TAGGED.format(method=method)))
    with pytest.raises(ValueError, match=refusal):
        ir.ModuleModel.of(module, "")


SPELLS = '''"""A body that spells a vocabulary nothing declares."""

from huggorm_dsl.declare import binding, header, spells


@header("nix/expr/eval.hh")
@binding(cxx="nix::EvalState", threading="pool", blocking=False)
class State:
    """The owner of the method under test."""

    @spells("Nowhere")
    def fail(self) -> None:
        """."""
'''


VOCABULARY = '''"""A vocabulary with something in it that is not a word."""

from enum import StrEnum

from huggorm_dsl.declare import words


@words()
class Mode(StrEnum):
    """One word, and one thing that is not."""

    FAST = "fast"
    """Quickly."""

    {extra}
'''


@pytest.mark.parametrize("extra", [
    "def slow(self) -> str:\n        return 'slow'",
    "SLOW: str",
    '"""A docstring under no word."""',
])
def test_a_vocabulary_refuses_what_is_not_a_word(
        tmp_path: pathlib.Path, extra: str) -> None:
    """A vocabulary body is words and their docstrings. The reader read
    those and skipped the rest, so a stage emitting the words from the
    model would have dropped anything else without a word."""
    from huggorm_dsl.read import DeclarationError, read

    source = VOCABULARY.format(extra=extra)
    with pytest.raises(DeclarationError, match="nothing else"):
        _ = read(_declaration(tmp_path, source)).classes


def test_the_model_refuses_a_spelling_of_no_vocabulary(
        tmp_path: pathlib.Path) -> None:
    """`@spells` must name an enum-backed vocabulary the declaration
    sees, and the model checks it before any surface is built
    (huggorm#115)."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, SPELLS))
    with pytest.raises(TypeError, match="Nowhere"):
        ir.ModuleModel.of(module, "")


SHAPE = '''"""A wire value whose shape cannot round-trip."""

from huggorm_dsl.declare import Cxx, Str, binding, header, wire_value


@header("nix/store/path.hh")
@binding(cxx="nix::Signature", threading="pool", blocking=False)
@wire_value(text="{text}")
class Signed:
    """A value, for a test that never compiles one."""

    def __init__(self, name: Str) -> None:
        """."""
        Cxx("new (self) nix::Signature{{name, {{}}}};")

    def name(self) -> Str:
        """."""
{extra}'''


@pytest.mark.parametrize(("text", "extra", "refusal"), [
    ("nothing", "", "names no accessor"),
    ("name", '    def sig(self) -> Str:\n        """."""\n', "@local"),
])
def test_the_model_refuses_a_value_that_cannot_round_trip(
        tmp_path: pathlib.Path, text: str, extra: str, refusal: str) -> None:
    """A `text=` that names no accessor, and a constructor that does not
    take every wire field, are refused by the model (huggorm#115)."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    source = SHAPE.format(text=text, extra=extra)
    module = read(_declaration(tmp_path, source))
    with pytest.raises(TypeError, match=refusal):
        ir.ModuleModel.of(module, "")


HELD_ELSEWHERE = '''"""A record part read through a member another type holds."""

from huggorm_dsl.declare import (
    Str, binding, header, local, produced, reads, wire_read, wire_value)


@produced
@binding(threading="pool", blocking=False)
@wire_value()
class Collected:
    """Collected paths, for a test that never compiles one."""

    @wire_read("held")
    def paths(self) -> list[Str]:
        """The paths."""

    @local
    @reads("paths", collection="nix::StringSet")
    def held(self) -> list[Str]:
        """The same paths, as the member holds them."""


@header("nix/store/store-api.hh")
@binding(cxx="nix::Store", threading="pool", blocking=False)
class Store:
    """What makes one."""

    def collect(self) -> Collected:
        """."""
'''


def test_a_part_read_elsewhere_is_rebuilt_from_its_own_parameter(
        tmp_path: pathlib.Path) -> None:
    """`_from_parts` takes one parameter per part, named after the
    part. A part read through another accessor is converted back from
    THAT parameter, not from the accessor's name, which names nothing
    inside the lambda. No corpus class has such a part with a
    member collection, so only this test reaches the branch."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir
    from huggorm_gen.cppgen import nbemit

    unit = ir.ModuleModel.of(
        read(_declaration(tmp_path, HELD_ELSEWHERE)), "")
    model = ir.Model({c.name: c for c in unit.classes}, {}, {},
                     {}, ir.Errors("", {}), (unit,))
    text = nbemit.Emitter(model, unit).bind_function(unit.classes[0])
    assert "as_set<nix::StringSet>(paths)" in text, text
    assert "as_set<nix::StringSet>(held)" not in text, text


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
    (huggorm#74). The failure looked exactly like a declaration with
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


@pytest.mark.parametrize("line", [
    "import os",
    "from os import path",
    "from . import sibling",
])
def test_a_declaration_imports_only_from_the_allowed_modules(
        tmp_path: pathlib.Path, line: str) -> None:
    """A declaration describes C++ and computes nothing, so it imports
    only the vocabulary, the other declarations, `typing` and `enum`
    (huggorm#123)."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError, match="imports only from"):
        read(_declaration(tmp_path, f"{line}\n"))


def test_a_declaration_that_will_not_import_is_refused(
        tmp_path: pathlib.Path) -> None:
    """The import error reaches a reader, with Python's own reason.

    `load` used to answer None here and the reader fell back to the
    tree alone. That reads as a working declaration for every file
    with no `NIX_VERSION` branch in it, which is all of them: the
    same nodes survive either way, so nothing said a word - and
    nothing said it about the files importing from it either
    (huggorm#82).

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

    huggorm#76 asked for the answer to be in a REFUSAL rather than
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
    assert "huggorm#76" in said, "and where the path ends"

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
    # that could never run (huggorm#75), and this is what says so.
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
    line, and was dropped whole in silence (huggorm#75). The behaviour
    changed there and no gate held it; this is that gate (huggorm#76)."""
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
# has no branch at all, so the losing arm is written here or it is
# never read (huggorm#81).
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
    """A `Corpus` over one declaration.

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

    Python resolves `NIX_VERSION` during the import, so the arm this
    build does not take is not in the module at all. The contents
    check reads the import, so it never sees that arm and needs no
    exemption for it.

    Written here because the corpus has no branch: `read.py` imports
    every declaration for exactly this reason and no declaration in
    this repo uses it, which is how the errors emitter came to see
    neither arm and say nothing (huggorm#73)."""
    have = _one_file_corpus(tmp_path, BRANCHED_ACCESSOR)
    mod = have.module("digest.py")
    kept = {m.name for m in mod.classes[0].methods}
    assert kept == {"base16", "here"}, "the import chose an arm"



def _versioned_body(body: str) -> str:
    """A one-method declaration whose body is `body`, indented."""
    inner = "\n".join("        " + line for line in body.strip().splitlines())
    return f'''"""One bound class whose body branches on the version."""

from huggorm_dsl.declare import NIX_VERSION, Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    def text(self) -> Str:
        """The digest as text."""
{inner}
'''


def test_a_body_takes_the_arm_its_nix_takes(tmp_path: pathlib.Path) -> None:
    """One body, two spellings of one call (huggorm#55).

    The arm is picked with the `NIX_VERSION` the import used, so the
    emitted C++ holds one call and nothing above the binding changes."""
    have = _one_file_corpus(tmp_path, _versioned_body('''
if NIX_VERSION >= (99, 0):
    Cxx("return self.future();")
elif NIX_VERSION >= (2, 0):
    Cxx("return self.now();")
else:
    Cxx("return self.past();")
'''))
    [method] = have.module("digest.py").classes[0].methods
    assert method.cxx_body is not None
    assert method.cxx_body.text.strip() == "return self.now();"


@pytest.mark.parametrize(("body", "refusal"), [
    ('''
if NIX_VERSION >= (2, 0):
    Cxx("return self.now();")
''', "needs an `else`"),
    ('''
if len("x") > 0:
    Cxx("return self.now();")
else:
    Cxx("return self.past();")
''', "tests NIX_VERSION"),
    ('''
if NIX_VERSION >= (2, 0):
    x = 1
else:
    Cxx("return self.past();")
''', "one Cxx"),
])
def test_a_versioned_body_that_says_more_is_refused(
        tmp_path: pathlib.Path, body: str, refusal: str) -> None:
    """An `if` with no `else` would leave one Nix a derived binding in
    silence; any other test would be a declaration that runs code."""
    from huggorm_dsl.read import DeclarationError

    with pytest.raises(DeclarationError, match=re.escape(refusal)):
        _one_file_corpus(tmp_path, _versioned_body(body)).module("digest.py")


def test_the_emitter_must_write_every_method_the_reader_kept() -> None:
    """The second seam: what the reader kept, and what the emitter wrote.

    Checked against the TEXT the emitter produced, not against a
    second walk of the same `Class` objects - that would be two views
    of one decision agreeing with itself, which is the blind spot the
    reader's contents check avoids on the other side.

    `pathinfo.py` because it is the class 075 lost an accessor from,
    and `nar_size` because it is a plain one: no marker, no
    exemption, nothing else to explain its absence."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import generate
    from huggorm_gen.cppgen.nbemit import extension

    have = corpus()
    mod = have.module("pathinfo.py")
    model = generate.declared_model()
    unit = model.module("pathinfo")
    bound = unit.bindable()
    text = extension(unit, "huggorm_bindings.pathinfo", model,
                     chain=[], errors="")
    generate.census_written(mod, bound, text)

    lost = text.replace('def("nar_size"', 'def("not_that_one"')
    assert lost != text, "the emitter no longer writes nar_size at all"
    with pytest.raises(TypeError, match="nar_size"):
        generate.census_written(mod, bound, lost)


def test_a_function_the_emitter_writes_another_way_is_not_missing() -> None:
    """Three declared functions that are not bound names, and none of
    them is exempt by name.

    `_init_libstore` is `@startup`, emitted as a CALL at module init;
    `_translate_nix_error` is a `@translator`, emitted as a
    registration; and `open_store` carries `@constructs(Store)`,
    emitted as that class's `nb::new_` - a caller
    writes `Store(uri)` and never the function. `store.py` has the
    first and the third, and `path.py` the first two.

    Without the third the census would fail the real corpus, which is
    how it was found."""
    from huggorm_decl import corpus
    from huggorm_gen.cppgen import generate
    from huggorm_gen.cppgen.nbemit import extension

    have = corpus()
    model = generate.declared_model()
    for stem, shapes in (("store", {"open_store", "_init_libstore"}),
                         ("path", {"_init_libstore", "_translate_nix_error"})):
        mod = have.module(f"{stem}.py")
        assert shapes <= {f.name for f in mod.functions}
        unit = model.module(stem)
        text = extension(unit, f"huggorm_bindings.{stem}", model,
                         chain=[], errors="")
        assert 'def("open_store"' not in text, "it is a constructor, not a name"
        generate.census_written(mod, unit.bindable(), text)


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
    a file a person maintains (huggorm#90).

    `decl/errors.py` now says `header = "nix/..."` beside each `cxx`,
    and the emitter writes the include beside the chain.

    NOT load-bearing, measured before this was written: removing all
    three from the header still compiled, because `path.cpp` reaches
    `nix::InvalidPath` through `nix/store/path.hh`'s own transitive
    include. So the compiler cannot gate this either, and the emitted
    TEXT is what can.

    A file with NO translator is the control. It catches nothing, so
    it asks for none of them."""
    from huggorm_gen.cppgen.generate import declared_model
    from huggorm_gen.cppgen.nbemit import extension

    headers = declared_model().errors.headers
    assert headers == ["huggorm_decl/cpp/eval_errors.hpp",
                       "nix/expr/eval-error.hh",
                       "nix/store/store-api.hh",
                       "nix/store/store-dir-config.hh",
                       "nix/util/error.hh",
                       "nix/util/signals.hh"], headers

    unit = declared_model().module("path")
    assert unit.translators, "the control below means nothing otherwise"
    text = extension(unit, "huggorm_bindings.path", declared_model(),
                     chain=[], errors="",
                     error_headers=headers)
    for h in headers:
        assert f'#include "{h}"' in text, h

    without = extension(unit, "huggorm_bindings.path", declared_model(),
                        chain=[], errors="")
    assert '#include "nix/store/store-dir-config.hh"' not in without, \
        "the headers come from the derivation, not from a fixed list"


def test_a_caught_error_must_say_which_header_declares_it(
        tmp_path: pathlib.Path) -> None:
    """Both directions refused, because either line alone reaches
    nothing.

    A `cxx` with no `header` is a catch whose type the emitted file
    can only reach by accident. A `header` with no `cxx` is a line no
    emitter reads, and this repo's named failure mode is a line
    nobody reads looking exactly like one nobody wrote.

    Drop either refusal and the matching half of this passes."""
    from huggorm_dsl.read import DeclarationError, read

    def errors(name: str, source: str) -> None:
        (tmp_path / name).mkdir()
        read(_declaration(tmp_path / name, source))

    with pytest.raises(DeclarationError, match="which header declares it"):
        errors("no_header", 'class NixError(Exception):\n'
                            '    cxx = "nix::Error"\n')

    with pytest.raises(DeclarationError, match="`header` with no `cxx`"):
        errors("no_cxx", 'class NixError(Exception):\n'
                         '    header = "nix/util/error.hh"\n')


def test_an_undecorated_class_is_an_exception(
        tmp_path: pathlib.Path) -> None:
    """A class with no decorator that derives from no exception is
    refused. The module transform would copy it through, and no model
    entry and no catch clause would know it - a silent skip."""
    from huggorm_dsl.read import DeclarationError, read

    path = _declaration(tmp_path, 'class NixError(Exception):\n'
                                  '    pass\n\n\n'
                                  'class Helper:\n'
                                  '    pass\n')
    with pytest.raises(DeclarationError, match="Helper: a class with no "
                                               "decorator declares an "
                                               "exception"):
        read(path)


def test_a_reader_reads_a_record(tmp_path: pathlib.Path) -> None:
    """A part past the message crosses through the reader template,
    which takes a record. A scalar there would only fail to compile."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    path = _declaration(tmp_path, READERLESS.replace(
        '    _wire', '    reader = "huggorm::error_info"\n    _wire', 1))
    with pytest.raises(TypeError, match="part 'code' is a scalar"):
        _errors(read(path), ir)


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
    text = pyerrors.module(tree, "doc", "pkg")
    lines = [ln.strip() for ln in text.splitlines()]
    assert not [ln for ln in lines if ln.startswith("header =")], text
    assert not [ln for ln in lines if ln.startswith("cxx =")], text
    assert not [ln for ln in lines if ln.startswith("reader =")], text
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

    before = ast.dump(tree)
    pyerrors.module(tree, "doc", "pkg")
    assert ast.dump(tree) == before, "the transform kept its hands off the tree"
    assert "nix::InvalidPath" in before


def test_a_body_brings_its_own_standard_header() -> None:
    """What a `Cxx` body SPELLS decides what the emitted file
    includes.

    `eval.py`'s bodies throw `std::invalid_argument` 54 times, and
    the emitted `eval.cpp` includes `<stdexcept>` because of that -
    not because a hand-written header carries one on its behalf.
    That was the arrangement huggorm#90 found: `eval.hpp` held a
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
    from huggorm_gen.cppgen.generate import declared_model
    from huggorm_gen.cppgen.nbemit import extension

    text = extension(declared_model().module("eval"), "huggorm_bindings.eval",
                     declared_model(), chain=[], errors="")
    assert "#include <stdexcept>" in text
    assert "std::invalid_argument" in text, "the body that needs it"

    other = extension(declared_model().module("pathinfo"),
                      "huggorm_bindings.pathinfo", declared_model(),
                      chain=[], errors="")
    assert "#include <cstdint>" in other
    assert "#include <stdexcept>" not in other, \
        "nothing in pathinfo throws, so nothing asks for it"


NESTED = '''"""An alias two containers down."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/store/derivations.hh")
@binding(cxx="nix::Thing", threading="pool", blocking=False)
class Thing:
    """Holds a map of lists."""

    def groups(self) -> dict[str, list[Str]] | None:
        """Each name, and what it holds."""
        Cxx("return self.groups;")
'''


def test_an_alias_is_resolved_at_any_depth(tmp_path: pathlib.Path) -> None:
    """The structure comes from the imported annotation, not its text.

    The reader used to peel ONE container off a spelling with
    `removeprefix`, so `dict[str, list[Str]]` reached the emitter with
    a bare `Str` it could not resolve."""
    from huggorm_dsl.read import Origin, read

    t = read(_declaration(tmp_path, NESTED)).classes[0].methods[0].ret
    assert t is not None
    assert (t.origin, t.required.origin, t.required.element.origin) == \
        (Origin.OPTIONAL, Origin.DICT, Origin.LIST)
    leaf = t.leaf
    assert leaf.python == "str"
    assert leaf.cxx is not None and leaf.cxx.spelling == "string"
    assert t.python == "dict[str, list[str]] | None"


QUOTED = '''"""A quoted annotation."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/store/derivations.hh")
@binding(cxx="nix::Thing", threading="pool", blocking=False)
class Thing:
    """Says its type as text."""

    def name(self) -> "Str":
        """Its name."""
        Cxx("return self.name;")
'''

UNIMPORTED = QUOTED.replace('-> "Str"', "-> Elsewhere")


@pytest.mark.parametrize(("source", "said"), [
    (QUOTED, "unquoted"),
    (UNIMPORTED, "Elsewhere"),
])
def test_an_annotation_the_import_cannot_resolve_is_refused(
        tmp_path: pathlib.Path, source: str, said: str) -> None:
    """A quote is text, and a name the file never imported arrives as
    a `ForwardRef`. The reader takes neither as a type."""
    from huggorm_dsl.read import DeclarationError, read

    with pytest.raises(DeclarationError, match=said):
        read(_declaration(tmp_path, source))


# A tree whose walk names what the class binds, or not. `{kinds}` is
# filled per case.
WALKED = '''"""One proxy that holds others."""

from huggorm_dsl.declare import I64, Cxx, Items, Leaf, Str, binding, header, tree


@header("nix/expr/value.hh")
@tree(kind="type_name", kinds={kinds})
@binding(cxx="nix::Value", threading="pool", blocking=True)
class Node:
    """A node of a tree."""

    def type_name(self) -> Str:
        """What this node is."""
        Cxx("return self.type_name();")

    def size(self) -> I64:
        """How many it holds."""
        Cxx("return self.size();")
'''


@pytest.mark.parametrize(("kinds", "refusal"), [
    ('{"list": Items(size="size", item="at")}', "names ['at']"),
    ('{"bytes": Leaf("bytes", "size")}', "a tree leaf is a bytes"),
])
def test_a_tree_that_names_what_the_class_lacks_is_refused(
        tmp_path: pathlib.Path, kinds: str, refusal: str) -> None:
    """The server walks a tree by the names `@tree` gives. A name the
    class does not bind would fail on the first walk, as an
    AttributeError a remote caller sees as an internal error; a leaf
    type with no arm would fail the same way in the codec. The build
    refuses both."""
    from huggorm_dsl.read import read
    from huggorm_gen import ir

    module = read(_declaration(tmp_path, WALKED.replace("{kinds}", kinds)))
    with pytest.raises(TypeError, match=re.escape(refusal)):
        ir.ClassModel.of(module.classes[0], "pkg", "mod",
                         ir.Resolver.of(module))
