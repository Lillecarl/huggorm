"""
A declaration file, read TWICE: imported, and parsed.

The IMPORT holds the FACTS and drives the read. `vars` gives every
name the build has, in definition order, with each `NIX_VERSION`
branch already chosen - Python resolves the branch, so nothing here
interprets a version condition. Bases, descriptors, signatures,
annotations and what each decorator wrote are all read off it, and a
name the reader can put nowhere is refused.

The TREE supplies TEXT the interpreter drops: the C++ inside a body,
docstrings (an attribute's included), where an import came from, and
the position a refusal points at. Each definition's node is found by
the line the import names, and `_reconcile` refuses a line the tree
has no node at (huggorm#123).

A body still never runs: `def` defines, it does not call. So `Cxx(...)`
in one is dead text this module lifts out of the tree, which is why a
declaration can name a C++ type this machine has never compiled.

## Why this matters more than it looks

The generator could build its model by IMPORTING the compiled
bindings and reflecting on them. That works, and it puts the whole
build in one order: compile the C++ first, learn what it says second.
Every surface above - async, protocols, RPC, stubs - would wait on a
C++ compiler.

Reading the declaration inverts that. The model is known BEFORE
anything compiles, because the declaration already says everything
the model holds. Then the binding and the model are two readings of
one document rather than two stages of a pipeline.

## The vocabulary

`declare.py` is not a declaration: it is a library of decorators and
dataclasses with no side effects, which this module imports the way a
type checker would.

And it earns its import. A decorator's job is to write a field on a
`Decl`, so rather than restate that mapping here - `header` sets
`.header`, `constructs(Store)` sets Store's `.factory` - this module
reads the `Decl` the import left on each class. The mapping lives in
one place, which is declare.py, and a decorator that gains an
argument needs no edit here.

## What it refuses

A name it cannot resolve, an annotation with no C++ spelling, a
decorator that is not from the vocabulary. Each stops with the line
number and the text that caused it. Guessing here would produce a
binding that compiles and is wrong.
"""

import annotationlib
import ast
import builtins
import contextlib
import difflib
import enum
import functools
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import os
import pathlib
import re
import sys
import types
import typing
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from types import ModuleType
from typing import Annotated, Any, get_args, get_origin, get_overloads

from huggorm_dsl import declare
from huggorm_dsl.declare import (
    Crossing,
    Cxx,
    Decl,
    DeclKind,
    Field,
    Subscription,
    Threading,
)

# Decorators that are Python's, not ours. A declaration may use them
# and they are read rather than applied.
BUILTIN_DECORATORS = frozenset({"property", "staticmethod", "classmethod",
                                "overload"})

# Every node that DEFINES a name. Stated once because
# `ast.AsyncFunctionDef` is not a subclass of `ast.FunctionDef`, and
# the three readers that ask this question have to agree: `_live`
# reads a definition's `co_firstlineno`, `_reconcile` checks that the
# tree has a node there, and `_resolve` keeps the arm the import
# chose. A reader short one of the three arms drops an `async def`
# out of one reading and not the other (huggorm#88).
DEFINITIONS = (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
type DefinitionNode = ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


# Where a declaration takes its vocabulary from. Named once: a
# declaration is read rather than imported, so this string is the only
# thing tying the two files together.
VOCABULARY = "huggorm_dsl.declare"

# `typing.Annotated`, which is where a fact about a TYPE goes. Named
# because the reader matches on the spelling in the source: the alias
# is never evaluated here, so `Annotated` is a bare name in a tree.

# Where the declarations live. A declaration that names a type
# another declaration owns imports it from here, and the reader
# follows that import rather than being told the file.
DECLARATIONS = "huggorm_decl.decl"

# Every module a declaration may import from, with its submodules.
# `enum` for a vocabulary's `StrEnum`; `pathlib` and `datetime` reach a
# declaration only through the vocabulary's aliases.
IMPORTABLE = (VOCABULARY, DECLARATIONS, "typing", "enum")

# What a hand-written wire reconstructor is called. One name, because
# the wire layer asks for it by that name and the emitter binds it by
# that name.
FROM_PARTS = "_from_parts"

# What a class decorator leaves on the class it marks. The reader
# reads both off the import, and the class read accepts both.
DECL = "_decl"
NEEDS = "_needs"

# The lines an exception declaration writes in its class body. `cxx`
# and `header` are the class's own; `reader` and `_wire_fields` are
# read off the import, so a subclass inherits them.
CXX = "cxx"
HEADER = "header"
READER = "reader"
WIRE_FIELDS = "_wire_fields"
# The parts `as_error` fills from `what()` itself.
MESSAGE_PARTS = 2

# The dunders a declaration may write as an ordinary method. Every
# emitter carries a method by name. Each one is added when a
# declaration needs it (huggorm#88).
DECLARED_DUNDERS = frozenset({"__call__"})


def is_surface(name: str) -> bool:
    """Whether a declared method is surface: not private, or a taught
    dunder. A leading underscore alone would drop `__call__`."""
    return not name.startswith("_") or name in DECLARED_DUNDERS


# Which declaration is being read, innermost last.
#
# A STACK rather than one path, because `_uses` calls `read` for every
# declaration this one imports - `decl/store.py` pulls in five - so an
# error is routinely raised while reading a file that is not the one
# the caller asked for. The old message said "line 183" and left a
# reader to work out which of nine files that was (huggorm#61).
#
# A plain list, not a ContextVar. Nothing here runs concurrently: the
# generator reads the corpus on one thread, and a ContextVar would buy
# isolation nobody has asked for while hiding the push/pop that makes
# this legible.
_READING: list[str] = []


class Diagnostics:
    """Every refusal one read produced, and what it made unsound.

    A declaration author fixing three mistakes wants three messages,
    not three runs. What stops that being an improvement is BOGUS
    messages - a consequence reported as a cause - so this carries the
    means to tell them apart rather than only a list.

    `unsound` is the name of every class whose reading failed. There
    is exactly ONE cross-class check in this reader - `_check_arms`,
    over the `resolvable` map `read` builds - so that set has exactly
    one consumer, and suppression is a single precise rule instead of
    a blanket.

    `blind` says an IMPORT failed. Then this file cannot know what the
    other declared, so an unresolvable arm here means nothing and the
    arm check says so once rather than guessing per arm.

    Deduplicated, because `_uses` calls `read` for each import and
    nothing caches it: `decl/path.py` is read by five declarations, so
    one mistake in it would otherwise be reported five times."""

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.unsound: set[str] = set()
        self.blind = False
        self._seen: set[str] = set()

    def record(self, err: Exception, unsound: str = "") -> None:
        text = str(err)
        if text not in self._seen:
            self._seen.add(text)
            self.messages.append(text)
        if unsound:
            self.unsound.add(unsound)

    def raise_if_any(self) -> None:
        if not self.messages:
            return
        if len(self.messages) == 1:
            raise DeclarationError.already(self.messages[0])
        joined = "\n  ".join(self.messages)
        raise DeclarationError.already(
            f"{len(self.messages)} declaration errors:\n  {joined}")


# The collector in force, or None to raise on the first refusal.
#
# None is the DEFAULT and it is the honest one: a caller that reads a
# single declaration wants the exception, and every test that asserts
# a refusal keeps working unchanged. Collection is something the
# generator opts into for a whole corpus read.
_COLLECTING: Diagnostics | None = None


@contextlib.contextmanager
def collecting() -> Iterator[Diagnostics]:
    """Gather refusals across one corpus read, then report them together.

    Re-entrant by design: the outermost collector wins, so `_uses`
    recursing into `read` contributes to one report rather than
    starting a second."""
    global _COLLECTING
    if _COLLECTING is not None:
        yield _COLLECTING
        return
    _COLLECTING = Diagnostics()
    try:
        yield _COLLECTING
    finally:
        done, _COLLECTING = _COLLECTING, None
        done.raise_if_any()


def _survive(err: DeclarationError, unsound: str = "") -> None:
    """Record and continue, or re-raise when nothing is collecting."""
    if _COLLECTING is None:
        raise err
    _COLLECTING.record(err, unsound)


@contextlib.contextmanager
def reading(path: str | os.PathLike[str]) -> Iterator[None]:
    """Name the declaration a diagnostic raised in here belongs to.

    THE ONLY PUSH. `read` and `resolved` called it for themselves,
    each with its own `append` / `try` / `finally` / `pop` - one fact
    written three times, which goal 3 says belongs in one place. They
    use this now.

    `str`, because `_READING[-1]` is concatenated with `:{line}` to
    build a diagnostic's position. The annotation said `list[str]`
    and nothing enforced it, so a caller passing a `pathlib.Path`
    - which every other function here tolerates, `load` does
    `pathlib.Path(path).stem` - got

        TypeError: unsupported operand type(s) for +=: 'PosixPath'
        and 'str'

    raised INSIDE the refusal, losing the message it was about to
    give. No caller in this repository does that: `huggorm_decl`
    normalises at its own boundary. Found from a probe, and fixed
    here rather than at the concatenation because this is where the
    value enters.

    So the PARAMETER says `os.PathLike` too, and that is not a
    widening for its own sake: this function already accepted one and
    mishandled it. A signature that admits what the body handles is
    the honest one, and it is what a gate can call without going
    off-contract."""
    _READING.append(str(path))
    try:
        yield
    finally:
        _READING.pop()


class DeclarationError(Exception):
    """A declaration this reader will not guess at.

    Carries `path:line:col`, because the reader's answer to an
    unreadable declaration is to point at it - and a tool that opens
    the file wants all three. The format is the one every compiler
    and every editor already parses.

    The column is 1-based. `ast` counts columns from 0 and lines from
    1, which is nobody's convention on either count; a message that
    mixes the two sends a reader one character to the left.

    The path comes from `_READING` rather than from an argument, so
    the 36 raise sites in this module stay as they are. Threading a
    path through 36 signatures to print it in one place is the shape
    this codebase spends its effort removing.

    It is a `str` because `reading` makes it one. Doing it here
    instead would be the same normalisation at every read of the
    stack rather than at the one write to it."""

    def __init__(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", None)
        col = getattr(node, "col_offset", None)
        where = _READING[-1] if _READING else "<declaration>"
        if line is not None:
            where += f":{line}"
            if col is not None:
                where += f":{col + 1}"
        super().__init__(f"{where}: {message}")

    @classmethod
    def already(cls, text: str) -> DeclarationError:
        """One already-formatted message, for the collector's report.

        `__init__` builds the position from a node, and a report has
        several positions in it - so this is the way to make one
        without a node to point at."""
        err = cls.__new__(cls)
        Exception.__init__(err, text)
        return err


class Origin(enum.StrEnum):
    """What a type node wraps. A leaf has no origin."""

    OPTIONAL = "optional"
    LIST = "list"
    DICT = "dict"


@dataclass(frozen=True)
class Type:
    """A declared type, from both sides of the boundary.

    `python` is what the annotation said, verbatim. `cxx` is the C++
    fact behind it, and it is None for a PRODUCED value - such a class
    holds no C++ object at all, so its fields are already Python and
    there is nothing for a C++ spelling to describe."""

    python: str
    cxx: Cxx | None = None
    # This type names another DECLARED class rather than a primitive.
    # The emitter resolves the C++ spelling through the other
    # declaration, so neither file repeats it.
    bound: bool = False
    # The STRUCTURE, which `python` only renders. None for a leaf; a
    # list, a dict or an optional is a node whose one argument is the
    # element, the map's value, or the type that may be absent. An
    # emitter walks these and never reads `python` to find them.
    origin: Origin | None = None
    args: tuple[Type, ...] = ()
    # The module an undeclared class comes from: "pathlib" for
    # `pathlib.Path`. Empty for a builtin and for a declared class.
    # Recorded while the reader holds the class, so no later stage
    # reads it back out of `python`.
    module: str = ""
    # How an ASYNC surface spells this leaf, from `Async(...)` on the
    # alias: "anyio.Path" for `Path`. Empty for most types.
    twin: str = ""

    @property
    def optional(self) -> bool:
        """Whether None is a legal value."""
        return self.origin is Origin.OPTIONAL

    @property
    def inner(self) -> Type:
        """What this node wraps. Every walk that follows one argument
        comes through here, so a new `Origin` fails type-check here
        rather than being skipped (huggorm#139)."""
        match self.origin:
            case None:
                raise TypeError(f"'{self.python}' wraps nothing")
            case Origin.OPTIONAL | Origin.LIST | Origin.DICT:
                return self.args[0]
            case _:
                typing.assert_never(self.origin)

    @property
    def required(self) -> Type:
        """This type without the None, or itself."""
        return self.inner if self.optional else self

    @property
    def container(self) -> bool:
        return self.origin in (Origin.LIST, Origin.DICT)

    @property
    def element(self) -> Type:
        """What a list holds, or a map's value."""
        if not self.container:
            raise TypeError(f"'{self.python}' is not a container")
        return self.inner

    @property
    def leaf(self) -> Type:
        """The type at the bottom of every container and optional."""
        t = self
        while t.origin:
            t = t.inner
        return t

    @property
    def wire(self) -> str:
        """The `_wire_fields` spelling of this type.

        Derived, not declared. `T | None` is `T?`, because that is how
        the wire says presence; everything else crosses as itself -
        except a WIDTH, which Python does not spell at all.

        Python has one integer type and C++ has two of 64 bits, and
        the wire needs the difference: a sint64 holds every int64_t
        and half of a uint64_t. So `int` here means the signed one and
        `uint` means the unsigned one, and the C++ spelling the alias
        already carries is what says which. Above the boundary both
        are still `int`; only the crossing knows the width
        (huggorm#79).

        The vocabulary says which: `U64` carries `Cxx(width="uint")`.
        `uint` must be one of `wiretypes.SCALAR_NAMES`, and
        `contracts.wire` refuses a field type it does not know."""
        held = self.required
        inner = held.python
        leaf = held.leaf
        if leaf.cxx is not None and leaf.cxx.width:
            if held.origin:
                # A CONTAINER of the width, such as `dict[str, U64]`.
                # The alias's C++ spelling reaches here attached to
                # the whole container - `type_of` puts it there - so
                # naming the container `uint` would say the dict is
                # one. Refused rather than passed through as `int`,
                # which is what it did before and would lose the top
                # half of every value in it.
                raise TypeError(
                    f"'{inner}' holds a {leaf.cxx.spelling}, and a "
                    f"container of a width has no wire spelling yet. "
                    f"See huggorm#79.")
            inner = leaf.cxx.width
        return f"{inner}?" if self.optional else inner


@dataclass(frozen=True)
class Param:
    """One declared parameter: its name, its type, and its default.

    `default` is the VALUE the imported signature holds, and
    `NO_DEFAULT` when there is none; None is a real default. A
    vocabulary member arrives as the string it is, because a word
    class's members are plain strings, so `member` keeps which one the
    declaration named: `BuildMode.NORMAL` is `"normal"` and `NORMAL`.

    Not unpackable. It unpacked as `(name, type)`, and an emitter that
    wrote `for n, _ in params` never saw a default: a constructor
    default reached no binding that way."""

    name: str
    type: Type
    default: Any = inspect.Parameter.empty
    member: str = ""

    @property
    def has_default(self) -> bool:
        """Whether a caller may leave this parameter out."""
        return self.default is not inspect.Parameter.empty


@dataclass(frozen=True)
class Method:
    """One declared method, with the C++ facts resolved.

    `params` and `ret` hold `Cxx`, not names: resolution happens once,
    here, so no emitter downstream has to know that `StrView` means
    `string_view` and owes the boundary a copy."""

    name: str
    # RAW, as written. Cleaning is a reader's convenience and an
    # emitter's problem: the stubs want the literal text a class
    # carries, and `_doc` re-indents from raw anyway.
    doc: str
    params: tuple[Param, ...]
    ret: Type | None
    cxx_name: str = ""
    blocks: bool = False
    # This one cannot wait, on a class whose calls generally can.
    instant: bool = False
    # Declared with Python's own @property: an ATTRIBUTE, not a call.
    # A value's parts read as attributes and a handle's actions read
    # as methods, and which one an accessor is was never a property
    # of its class - nanopynix presents StorePath.to_string() as a
    # method and ValidPathInfo.path as an attribute, and both are
    # values. So the declaration says it, in the word Python already
    # has for it.
    #
    # NO EMITTER HONOURS IT YET. `nbemit` refuses a bound class that
    # sets it, because the stub, `_parts` and the wire all call an
    # accessor and a binding alone cannot change that (huggorm#76).
    # The word stays because the reader is where it is read and the
    # refusal is what reads it.
    prop: bool = False
    # The C++ function a FREE function binds, from @binds.
    binds: str = ""
    # The C++ data member behind this name, from @reads. Empty when
    # the accessor is a call rather than a field.
    reads: str = ""
    # The C++ container that member IS, from @reads(collection=...).
    # Empty for a plain vector, and for every accessor whose element
    # class already states it. See `reads` in declare.py.
    member_collection: str = ""
    # Verbatim C++ for an accessor nothing can derive, from its body's
    # `Cxx(...)`.
    cxx_body: Body | None = None
    # The arm NAME this accessor needs, from @guard. Resolved to an
    # enumerator through the class's @arms table, so the C++ fact
    # lives in one place. The emitter writes the check.
    guard: str = ""
    # This accessor answers which arm is held, from @names. Emitted
    # from the same table the guards read.
    names: bool = False
    # The C++ initialiser this method calls to make a value, from
    # @produces. The emitter owns allocating, rooting and wrapping.
    produces: str = ""
    # This method mutates a value the binding BUILT, from @fills. The
    # value names the method that makes one, for the refusal.
    fills: tuple[str, str] | None = None
    # For a caller, not for the wire, from @local. A wire value
    # crosses as every accessor it has, so this is the one thing an
    # accessor has to be able to say for itself.
    local: bool = False
    # `@virtual`: a Python subclass may override it.
    virtual: bool = False
    # `@posted`: Nix need not wait for an async override.
    posted: bool = False
    # The `@local` accessor the wire reads this part through, from
    # @wire_read. Empty means this accessor itself.
    wire_read: str = ""
    # Headers this method's BODY needs, beyond its class's, from
    # @needs. Empty when the signature already names everything.
    headers: tuple[str, ...] = ()
    # Vocabularies this method's BODY spells, beyond its signature,
    # from @spells. `@needs` one level up: a body that builds a word
    # the signature never mentions still wants the conversion.
    spells: tuple[str, ...] = ()
    # Run once at module import, and do not export, from @startup.
    startup: bool = False
    # Register as the module's exception translator, from @translator.
    translator: bool = False
    # The threading policy a FREE function opts into, from @threading.
    # None means it declared none, which is what keeps a runtime
    # helper out of every generated form.
    policy: Threading | None = None
    # What the call does to a subscription a server shares, from
    # @subscription.
    subscription: Subscription | None = None


@dataclass(frozen=True)
class Member:
    """One word of a vocabulary: the name, the string, and why.

    `doc` is the docstring written UNDER the assignment, which is
    where Python puts an attribute's own documentation. Empty for a
    member whose name says the whole of it."""

    name: str
    value: str
    doc: str = ""


@dataclass(frozen=True)
class Raised:
    """What an exception declaration says beyond its name."""

    # Bases declared in the same file, in order. Not `Exception`.
    bases: tuple[str, ...]
    # What crosses the wire, in constructor order.
    parts: tuple[tuple[str, Type], ...] = ()
    # The C++ class the translator catches, and the header that
    # declares it. Both empty for an exception only this binding raises.
    cxx: str = ""
    header: str = ""
    # The C++ template that reads each part past the message off the
    # caught exception, given the part's record type.
    reader: str = ""


@dataclass(frozen=True)
class Class:
    """One declared class: what it says, and what its decorators said."""

    name: str
    doc: str
    decl: Decl
    ctor: Method | None
    methods: tuple[Method, ...] = ()
    # The words, for a vocabulary. Empty for every other kind.
    members: tuple[Member, ...] = ()
    # The declaration file that declared it, without the suffix. The
    # emitters name a module after its declaration, so this is also
    # the module the class lands in - and a class that arrived
    # through `uses` carries the OTHER file's name, which is the only
    # way an emitter can write the import that reaches it.
    module: str = ""
    # How to rebuild one from the parts that crossed the wire, when
    # the declaration says. None for a value whose C++ constructor
    # already takes exactly those parts: naming the constructor is
    # then the whole helper, and the emitter writes it.
    from_parts: Method | None = None
    # The C++ facts of an exception. None for every other kind.
    raised: Raised | None = None

    @property
    def is_words(self) -> bool:
        """Whether this is a vocabulary rather than a binding.

        A StrEnum with no C++ object behind it. It crosses as the
        string a member already IS, so nothing about it compiles."""
        return self.decl.kind is DeclKind.WORDS

    @property
    def is_union(self) -> bool:
        """Whether this is a SUM of other declared types.

        Written as a module-level alias - `DerivedPath = StorePath |
        DerivedPathBuilt` - so it declares no methods and binds no C++
        class of its own. It is a Class here anyway, because that is
        what makes every emitter able to NAME it through `known`
        without learning a second kind of thing."""
        return self.decl.kind is DeclKind.UNION

    @property
    def is_value(self) -> bool:
        """Whether this class holds Python slots and no C++ at all.

        Two facts, not one, and an earlier version read `@produced`
        as if it were both. `@produced` says only that nothing
        constructs one. `@binding(cxx=...)` says there is a C++ object
        behind it. PathInfo has the first and not the second, so it is
        a value; nix::Store has both, so it is a handle a factory
        opens - and emitting it as a value produced a module with the
        class in it twice."""
        return self.decl.produced and not self.decl.cxx

    @property
    def is_produced(self) -> bool:
        """Whether nothing a caller writes can build one.

        Two halves, and `is_value` stood in for both until a produced
        value bound a real Nix type. Something else makes one -
        `@produced` - AND this declaration offers no way in.

        `nix::Store` has the first half and not the second: it
        declares an `__init__`, and the emitter binds `open_store`
        behind it, so `Store(uri)` works and no stub may say NoReturn.

        `ctor is None` is the same test the emitter makes when it
        decides whether to write a constructor at all, so the surface
        and this cannot disagree."""
        return self.decl.produced and self.ctor is None

    @property
    def constructs(self) -> bool:
        """Whether Python has a way to make one - "is there a door".

        The DERIVED question, computed once here because three layers
        used to ask it and all three asked `abstract` instead
        (huggorm#61). `@abstract` states a fact about C++: the type has
        pure virtuals. Whether a caller can write `Store(uri)` is a
        different question, and conflating them meant the declaration
        could not state the true fact about nix::Store without
        deleting its constructor.

        Three ways to have no door, and each is a different sentence:

        - no `__init__` at all, so nothing was declared to call;
        - `@abstract` with no factory, so there is nothing to make;
        - `@produced` and no `__init__`, which is the pair
          `is_produced` names - covered by the first test here.

        A FACTORY answers abstractness. `nix::Store` is abstract and
        `nix::openStore` hands back a concrete `LocalStore` or
        `UDSRemoteStore`, so the door is open and the C++ fact is
        still true."""
        if self.ctor is None:
            return False
        return bool(self.decl.factory) or not self.decl.abstract

    @property
    def parts(self) -> list[tuple[Field, Method]]:
        """Every declared part of a wire value, with the accessor it reads.

        Two sources, and which one applies is a real difference. A value
        that DECLARES its parts says which accessors they are and in what
        order - selection and order, and nothing else. A RECORD declares
        none and needs none: the emitter wrote the struct, so every
        accessor is a field.

        A declared part is either a `Field` or a plain NAME. The name is
        the common case and it restates nothing: the part is called what
        the accessor is called, and its type is the annotation the
        accessor already carries. `Field` is for the other case, where the
        two names differ - a StorePath's part is `base_name` and is read
        by `to_string`.

        A value that declares NO parts is every accessor, in declaration
        order, minus the ones marked `@local`. Listing ten names that
        are already ten `def`s one screen above is a second declaration
        of one fact; an accessor kept OFF the wire says so where it is
        written, which is the only place that cannot drift from it.

        The accessor comes back beside the field because a type is spelled
        two ways at this boundary. The WIRE spelling is what the model
        carries; the C++ spelling is what `_from_parts` takes, and only
        the accessor's own annotation has it."""
        by_name = {m.name: m for m in self.methods}
        if self.decl.fields:
            if marked := [m.name for m in self.methods if m.wire_read]:
                raise TypeError(
                    f"{self.name}: {marked} carry @wire_read, which a "
                    f"declared field list ignores. Use one or the other.")
            out = []
            for f in self.decl.fields:
                if isinstance(f, str):
                    f = Field(f, read=f)
                m = by_name.get(f.read)
                if m is None or m.ret is None:
                    raise TypeError(
                        f"{self.name}: '{f.name}' is declared a wire field "
                        f"and '{f.read}' names no accessor of this class "
                        f"that answers anything.")
                out.append((f, m))
            return out
        if self.decl.wire is not Crossing.VALUE:
            return []
        out = []
        for m in self.methods:
            if m.ret is None or m.local:
                continue
            if not m.wire_read:
                out.append((Field(m.name, read=m.name), m))
                continue
            reader = by_name.get(m.wire_read)
            if reader is None or reader.ret is None or not reader.local:
                raise TypeError(
                    f"{self.name}.{m.name}: @wire_read names "
                    f"'{m.wire_read}', which must be a @local accessor "
                    f"of this class that answers something.")
            out.append((Field(m.name, read=m.wire_read), reader))
        return out


@dataclass(frozen=True)
class Module:
    """One declaration file."""

    name: str
    doc: str
    classes: tuple[Class, ...] = ()
    functions: tuple[Method, ...] = ()
    # The SUM types this declaration declares, as aliases. Kept apart
    # from `classes` because nothing binds one: a union names other
    # types and has no C++ class of its own.
    unions: tuple[Class, ...] = ()
    # Classes this declaration NAMES but does not declare, from
    # another declaration it imported. A vocabulary lives in its own
    # file and several bindings take one, so the alternative was
    # every emitter guessing which other files to read.
    uses: dict[str, Class] = field(default_factory=dict)
    # The EXCEPTION classes this declaration declares, in declared
    # order. Apart from `classes` because nothing binds one: the
    # translator CATCHES its C++ class, and its Python body is copied
    # through as written.
    errors: tuple[Class, ...] = ()

    @property
    def startup(self) -> tuple[Method, ...]:
        """What runs once when the module is imported."""
        return tuple(fn for fn in self.functions if fn.startup)

    @property
    def translators(self) -> tuple[Method, ...]:
        """The C++ that turns a library exception into a Python one."""
        return tuple(fn for fn in self.functions if fn.translator)

    @property
    def exported(self) -> tuple[Method, ...]:
        """The free functions the module actually offers a caller.

        A startup hook and a translator are declared here because
        this is where a module's C++ facts live, and neither is
        surface: one runs before a caller exists and the other runs
        instead of one.

        Nor is a factory whose class declares a constructor. It is
        bound as that class's `__new__`, so `Store(uri)` is the one
        way in, and exporting it beside that would be a second
        spelling of the same call. `parse_store_reference` makes a
        `StoreReference`, which declares none, so it stays."""
        made = {c.decl.factory for c in self.classes if c.ctor is not None}
        return tuple(fn for fn in self.functions
                     if not (fn.startup or fn.translator)
                     and fn.name not in made)

    @property
    def known(self) -> dict[str, Class]:
        """Every type this declaration can name, by name.

        Unions among them, because an annotation names one the same
        way it names a class and every emitter resolves it the same
        way."""
        return {**self.uses,
                **{c.name: c for c in self.classes},
                **{e.name: e for e in self.errors},
                **{u.name: u for u in self.unions}}
    # Local name -> name in declare. `from declare import Str as S`
    # is legal Python, so the reader follows the import rather than
    # matching the spelling it expects.
    vocabulary: dict[str, str] = field(default_factory=dict)


# -- the vocabulary -------------------------------------------------------

def _vocabulary(tree: ast.Module) -> dict[str, str]:
    """Every name this file took from declare, as local -> declared.

    Only the vocabulary module counts. A declaration that imports
    anything else is not refused here - it may import for a type
    checker's sake - but nothing outside the vocabulary can decorate
    or annotate, and the resolvers below say so."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == VOCABULARY:
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out




def type_of(ann: object, node: ast.AST, home: Mapping[str, Any]) -> Type:
    """One annotation, as the object the import resolved it to.

    Nothing here reads text. Python 3.14 defers annotations, and
    `annotationlib` evaluates them once the declaration has run, so an
    alias such as `Str` arrives as `Annotated[str, Cxx("string")]`, a
    declared class as the class, and `list[...]`, `dict[str, ...]` and
    `T | None` as the generics they are. `get_origin` and `get_args`
    say the structure; the reader never parses a spelling to find it.

    `home` is the globals of the file the annotation is written in.
    They are where a UNION alias gets its name: a union is an
    `Annotated` object and does not know what it is called, so the
    name is whichever global of the declaring file IS that object.

    Refused, each for its own reason: a string (the quote is what made
    this reader parse text, and 3.14 needs none), a name the file never
    imported (it arrives as a `ForwardRef`), an inline union of
    anything but None, and a generic this binding has no container
    for."""
    if isinstance(ann, str):
        raise DeclarationError(
            node, f"'{ann}' is quoted. Write the annotation unquoted: "
                  f"Python 3.14 defers it, so a forward reference needs "
                  f"no quotes, and the reader takes the object, not text.")
    if isinstance(ann, annotationlib.ForwardRef):
        raise DeclarationError(
            node, f"'{ann.__forward_arg__}' is not a name this file defines "
                  f"or imports. Import the declaration that declares it.")
    origin = get_origin(ann)
    if origin is Annotated:
        held, *meta = get_args(ann)
        if any(isinstance(m, declare.Variant) for m in meta):
            name = next((k for k, v in home.items() if v is ann),
                        None)
            if name is None:
                raise DeclarationError(
                    node, "a union is named by the alias it is assigned "
                          "to, and this one is not a global of the file.")
            return Type(python=name, bound=True)
        twin = next((m.spelling for m in meta
                     if isinstance(m, declare.Async)), "")
        for m in meta:
            if isinstance(m, Cxx):
                return replace(_undeclared(held, m), twin=twin)
        raise DeclarationError(
            node, f"'{ann}' carries no C++ spelling. Annotate the alias "
                  f"with Cxx(...) in declare.py.")
    if origin in (types.UnionType, typing.Union):
        arms = get_args(ann)
        present = [a for a in arms if a is not type(None)]
        if len(arms) != 2 or len(present) != 1:
            raise DeclarationError(
                node, f"'{ann}': an annotation holds `T | None` and no other "
                      f"union. A sum type is a named alias with Variant(...).")
        inner = type_of(present[0], node, home)
        return Type(python=f"{inner.python} | None", origin=Origin.OPTIONAL,
                    args=(inner,))
    if origin is list:
        (item,) = get_args(ann)
        inner = type_of(item, node, home)
        return Type(python=f"list[{inner.python}]", origin=Origin.LIST,
                    args=(inner,))
    if origin is dict:
        key, value = get_args(ann)
        if key is not str:
            raise DeclarationError(
                node, f"'{ann}': a map is keyed by str. The wire has no "
                      f"other key, and a Nix attribute name is one.")
        inner = type_of(value, node, home)
        return Type(python=f"dict[str, {inner.python}]", origin=Origin.DICT,
                    args=(inner,))
    if origin is not None:
        raise DeclarationError(
            node, f"'{ann}': no binding carries a {origin.__name__}. A "
                  f"declaration holds list, dict[str, ...] and T | None.")
    if isinstance(ann, type):
        if _declared(ann, home):
            return Type(python=ann.__name__, bound=True)
        return _undeclared(ann)
    raise DeclarationError(node, f"'{ann!r}' is not a type.")


def _writable_default(value: object, node: ast.AST, where: str) -> None:
    """Refuse a default the surfaces cannot write as source. Each
    surface writes its own copy, so `[]` is one shared list per
    surface, and `repr(float("inf"))` is a NameError where it lands."""
    if isinstance(value, (list, dict, set, bytearray)):
        raise DeclarationError(
            node, f"{where}: {value!r} is a mutable default, and every "
                  f"generated surface would carry its own. Default to "
                  f"None: a list reads an absent argument back as empty.")
    try:
        same = ast.literal_eval(repr(value)) == value
    except (ValueError, SyntaxError):
        same = False
    if not same:
        raise DeclarationError(
            node, f"{where}: default {value!r} is not a literal the "
                  f"generated surfaces can write.")


def _member(ann: object, value: object) -> str:
    """Which member of a vocabulary a default names, or "".

    A word class holds plain strings, so the import turns
    `BuildMode.NORMAL` into `"normal"`. The declared type is the class
    that says which member that is."""
    if not isinstance(ann, type) or not isinstance(value, str):
        return ""
    decl = vars(ann).get(DECL)
    if decl is None or decl.kind is not DeclKind.WORDS:
        return ""
    return next((k for k, v in vars(ann).items()
                 if not k.startswith("_") and v == value), "")


def _undeclared(cls: type, cxx: Cxx | None = None) -> Type:
    """A class no declaration declares.

    Spelled bare when it is a builtin, and by its module otherwise,
    because `Path` alone is ambiguous and `pathlib.Path` is what an
    annotation has to say to typecheck."""
    if cls.__module__ == "builtins":
        return Type(python=cls.__name__, cxx=cxx)
    return Type(python=f"{cls.__module__}.{cls.__name__}", cxx=cxx,
                module=cls.__module__)


def _declared(cls: type, home: Mapping[str, Any]) -> bool:
    """Whether a class comes from a declaration, and so is BOUND.

    By where it was written, not by its name: a class defined in a file
    beside the one being read is declared, whatever it is called. The
    exceptions in `errors.py` are declared this way too, and carry no
    marker of their own.

    The file's own module first: `load` runs a file outside the
    declarations package under a name `sys.modules` does not hold, so
    `inspect.getfile` cannot find a class that file defines."""
    if cls.__module__ == home.get("__name__"):
        return True
    file = home.get("__file__")
    with contextlib.suppress(TypeError):
        if file:
            here = pathlib.Path(file).resolve().parent
            return pathlib.Path(inspect.getfile(cls)).resolve().parent == here
    return False


# -- literals -------------------------------------------------------------



# -- decorators -----------------------------------------------------------

def _check_markers(decorators: list[ast.expr], kind: str) -> None:
    """Check every marker against `declare.MARKERS`.

    One loop over a table, rather than a hand-written `if` per rule.
    The raises this file carries are not all replaced - most are
    about SHAPE, like "a body is a docstring then at most one Cxx" -
    but the ones about WHERE a marker is legal collapse into here.

    Unknown markers get a suggestion. `@read` for `@reads` is the
    mistake this pays for the first time somebody makes it."""
    seen: dict[str, ast.expr] = {}
    for node in decorators:
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            called = True
        elif isinstance(node, ast.Name):
            name, called = node.id, False
        else:
            name = ""
        if not name:
            # The import would still apply `@d.reads(...)`, and every
            # reader of the AST would miss it (huggorm#140).
            raise DeclarationError(
                node, "write a decorator as a bare name: "
                "import the marker, and use @reads, not @d.reads")
        if name in BUILTIN_DECORATORS:
            continue
        rule = declare.MARKERS.get(name)
        if rule is None:
            near = difflib.get_close_matches(name, declare.MARKERS, 1, 0.6)
            hint = f" - did you mean @{near[0]}?" if near else ""
            raise DeclarationError(node, f"unknown marker @{name}{hint}")
        if kind not in rule.target:
            legal = ", ".join(sorted(rule.target))
            raise DeclarationError(
                node, f"@{name} is not legal on a {kind} (legal on {legal})")
        if rule.arity == "flag" and called:
            raise DeclarationError(node, f"@{name} takes no arguments")
        if rule.arity != "flag" and not called:
            raise DeclarationError(node, f"@{name} needs arguments")
        if rule.arity != "repeatable" and name in seen:
            raise DeclarationError(node, f"@{name} may appear once")
        seen[name] = node
    for name, node in seen.items():
        rule = declare.MARKERS[name]
        for other in sorted(rule.excludes & seen.keys()):
            # Once per pair, not twice: the same conflict read from
            # both ends is one mistake.
            if name < other:
                raise DeclarationError(
                    node, f"@{name} and @{other} cannot both be set")
        for other in sorted(rule.requires - seen.keys()):
            raise DeclarationError(node, f"@{name} needs @{other}")




# -- methods --------------------------------------------------------------

# What a version test may use: `NIX_VERSION`, the `NIX_2_35` names,
# tuples of integers, and comparisons joined by `and`, `or` and `not`. Anything else in the
# test of a body's `if` is a declaration pretending to be a program.
_VERSION_TEST_NODES = (ast.Expression, ast.Compare, ast.BoolOp, ast.UnaryOp,
                       ast.Name, ast.Load, ast.Tuple, ast.Constant,
                       ast.Lt, ast.LtE, ast.Gt, ast.GtE, ast.Eq, ast.NotEq,
                       ast.And, ast.Or, ast.Not)


_VERSION_NAMES = ("NIX_VERSION", "NIX_2_35", "NIX_2_36")


def _version_holds(node: ast.FunctionDef, test: ast.expr) -> bool:
    """Evaluate the test of a body's `if` against this build's Nix."""
    tree = ast.Expression(test)
    for n in ast.walk(tree):
        if not isinstance(n, _VERSION_TEST_NODES) or (
                isinstance(n, ast.Name) and n.id not in _VERSION_NAMES) or (
                isinstance(n, ast.Constant) and not isinstance(n.value, int)):
            raise DeclarationError(
                test, f"{node.name}: a body's `if` tests NIX_VERSION "
                      f"against tuples of integers, and nothing else. "
                      f"{ast.unparse(test)!r} is more.")
    code = compile(tree, "<version test>", "eval")
    # The walk above admits NIX_VERSION, integer tuples and comparisons.
    return bool(eval(code, {"__builtins__": {}},
                     {name: getattr(declare, name) for name in _VERSION_NAMES}))


@dataclass(frozen=True)
class Body:
    """C++ a declaration carries, and where its first line is written.

    The emitter writes `#line` from `path` and `line`, so the compiler
    reports an error in a body at the declaration, not in the emitted
    `.cpp`."""

    text: str
    # Relative to the package root when the file is in one, so the
    # emitted text holds no store path.
    path: str
    line: int


def _source(path: str) -> str:
    marker = "/huggorm_decl/"
    at = path.rfind(marker)
    return path[at + 1:] if at >= 0 else path


def _cxx_call(node: ast.FunctionDef, stmt: ast.stmt) -> Body | None:
    """The C++ of one `Cxx("...")` statement, or None for another kind."""
    if not isinstance(stmt, ast.Expr):
        return None
    value = stmt.value
    if not (isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "Cxx"):
        return None
    if len(value.args) != 1 or not isinstance(value.args[0], ast.Constant):
        raise DeclarationError(
            stmt, f"{node.name}: Cxx() takes one string "
                  f"literal. The C++ is carried, not built.")
    literal = value.args[0]
    text = str(literal.value)
    # The literal starts on its own line; the C++ starts after the
    # newlines that `.strip()` drops.
    skipped = text[:len(text) - len(text.lstrip())].count("\n")
    return Body(text, _source(_READING[-1]) if _READING else "<declaration>",
                literal.lineno + skipped)


def _arm(node: ast.FunctionDef, stmts: list[ast.stmt]) -> Body:
    """The C++ of one arm of a body's `if`: one `Cxx(...)`, or a
    further `if`."""
    if len(stmts) == 1:
        if (cxx := _cxx_call(node, stmts[0])) is not None:
            return cxx
        if isinstance(stmts[0], ast.If):
            return _versioned(node, stmts[0])
    raise DeclarationError(
        stmts[0], f"{node.name}: each arm of a body's `if` is one "
                  f"Cxx(...), or another `if`.")


def _versioned(node: ast.FunctionDef, stmt: ast.If) -> Body:
    """The C++ of the arm this build's Nix takes."""
    if not stmt.orelse:
        raise DeclarationError(
            stmt, f"{node.name}: a body's `if` needs an `else`, or one "
                  f"Nix gets no body and the binding is derived in "
                  f"silence.")
    if _version_holds(node, stmt.test):
        return _arm(node, stmt.body)
    return _arm(node, stmt.orelse)


def _body(node: ast.FunctionDef) -> Body | None:
    """The C++ this method carries, read from its BODY.

    A declaration is Python, so C++ that a person writes goes where a
    person writes code:

        def get_uri(self) -> Str:
            # ...docstring here...
            Cxx("return self.config.getHumanReadableURI();")

    It never runs. `def` defines; it does not call - so `Cxx(...)`
    here is dead text this lifts out of the tree, exactly as a
    decorator argument was. That is also why `Cxx` does not have to
    be constructable: nothing constructs one.

    The grammar is small on purpose, and enforced rather than
    documented: a docstring, then at most ONE `Cxx(...)`, and nothing
    else. Anything more is a declaration pretending to be a program,
    and the emitter has no way to render it.

    The one `Cxx(...)` may sit in an `if NIX_VERSION ...` with an
    `else`, when one Nix spells the same call differently and nothing
    above the binding can tell (huggorm#55). The arm is chosen here,
    against the same `NIX_VERSION` the import used.

    An empty body is what says the emitter DERIVES the whole binding,
    which is fourteen of the thirty-eight. Presence of `Cxx` is the
    whole distinction, and unlike a decorator it cannot be
    half-stated - there is no way to write the marker and forget the
    body, or the body and forget the marker."""
    seen: Body | None = None
    for i, stmt in enumerate(node.body):
        if (i == 0 and isinstance(stmt, ast.Expr)
                and isinstance(stmt.value, ast.Constant)
                and isinstance(stmt.value.value, str)):
            continue                           # the docstring
        cxx = _cxx_call(node, stmt)
        if cxx is None and isinstance(stmt, ast.If):
            cxx = _versioned(node, stmt)
        if cxx is not None:
            if seen:
                raise DeclarationError(
                    stmt, f"{node.name}: one Cxx(...) per body. Two "
                          f"bodies is two bindings.")
            seen = cxx
            continue
        if (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is Ellipsis):
            continue                           # `...`, an empty body
        raise DeclarationError(
            stmt, f"{node.name}: a declaration body is a docstring, then "
                  f"at most one Cxx(...). {ast.unparse(stmt)!r} is "
                  f"neither, and nothing would emit it.")
    return seen




def _method(node: ast.FunctionDef, vocab: dict[str, str],
            fns: dict[int, Any],
            bound: bool = True, bound_kind: bool = True) -> Method:
    """One declared function.

    `bound=False` for a function with no `self` to skip. That is not
    the same question as WHERE it lives: `_from_parts` is a class
    member with no self, so `bound_kind` carries the marker-table
    kind separately.
    Reading a free function as a method silently drops its first
    parameter, which is how `open_store(uri)` first emitted without
    the `"uri"_a` that makes the parameter usable by keyword.

    A class member with no self says so with `@staticmethod`, and
    must: without it a type checker reads the first parameter as
    self, which is the same mistake from the other side."""
    # What the import holds at this line: the function, or the
    # descriptor wrapping it. The descriptor is the fact a decorator
    # spelling only names.
    held = fns.get(_first_line(node))
    fn = getattr(held, "fget", None) or getattr(held, "__func__", held)
    if not isinstance(fn, types.FunctionType):
        raise DeclarationError(
            node, f"{node.name}: the import has no function at this line, "
                  f"so its annotations cannot be resolved.")
    static = not bound and bound_kind
    if static and not isinstance(held, staticmethod):
        raise DeclarationError(
            node, f"{node.name}: a class member with no self is a "
                  f"@staticmethod. Say so, or a type checker reads its "
                  f"first parameter as self.")
    if isinstance(held, staticmethod | classmethod) and not static:
        # `@staticmethod` and `@classmethod` say the first parameter
        # is not `self`, and this reads a bound method by SKIPPING the
        # first parameter. So the one below would be dropped, and every
        # emitter would then write a signature short of an argument -
        # the shape of the `open_store(uri)` bug this function's own
        # docstring records.
        #
        # Refused rather than honoured, because no emitter has a word
        # for either: nanobind spells them `def_static` and a
        # classmethod not at all, the stub would need the same
        # decorator, and `_parts` fetches an accessor off an INSTANCE.
        # That is four outputs for a shape no declaration wants yet
        # (huggorm#76).
        raise DeclarationError(
            node, f"{node.name}: @{type(held).__name__} has no meaning in "
                  f"a declaration yet, and this reads a method as one "
                  f"that takes self - so its first parameter would be "
                  f"dropped. See huggorm#76.")
    signature = inspect.signature(
        fn, annotation_format=annotationlib.Format.FORWARDREF)
    every = list(signature.parameters.values())
    if any(p.kind is not inspect.Parameter.POSITIONAL_OR_KEYWORD
           for p in every):
        raise DeclarationError(
            node, f"{node.name}: a bound method takes plain positional "
                  f"parameters. C++ has no *args.")
    anns = annotationlib.get_annotations(
        fn, format=annotationlib.Format.FORWARDREF)
    # The tree only for positions: a refusal points at the parameter.
    at = {a.arg: a for a in node.args.args}
    params = []
    for p in every[1:] if bound else every:
        arg = at[p.name]
        if p.name not in anns:
            raise DeclarationError(
                arg, f"{node.name}({arg.arg}): every parameter states its "
                     f"type.")
        default = p.default
        declared = type_of(anns[arg.arg], arg, fn.__globals__)
        if default is None and not declared.optional:
            # An implicit Optional: every surface but the C++ then says
            # `| None` where the declaration did not (huggorm#104).
            raise DeclarationError(
                arg, f"{node.name}({arg.arg}): a default of None needs "
                     f"`| None` in the type.")
        member = _member(anns[arg.arg], default)
        # A vocabulary member is written as the member, not by repr.
        if default is not inspect.Parameter.empty and not member:
            _writable_default(default, arg, f"{node.name}({arg.arg})")
        params.append(Param(arg.arg, declared, default, member))

    ret: Type | None = None
    if node.returns is not None and anns.get("return") is not None:
        ret = type_of(anns["return"], node.returns, fn.__globals__)

    # A method decorator writes an attribute on the function, and the
    # import already ran it, so the markers are read off `fn`.
    _check_markers(node.decorator_list, "method" if bound_kind else "free")
    marked = fn
    if getattr(marked, "_reads", "") and params:
        # A data member is READ, not called, so there is nowhere for
        # an argument to go. The emitter drops them silently, which
        # would leave the Python signature demanding a value nothing
        # uses.
        raise DeclarationError(
            node, f"{node.name}: @reads names a data member, so this "
                  f"accessor takes no parameters.")
    return Method(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        params=tuple(params),
        ret=ret,
        cxx_name=getattr(marked, "_cxx_name", ""),
        guard=getattr(marked, "_guard", ""),
        names=bool(getattr(marked, "_names", False)),
        produces=getattr(marked, "_produces", ""),
        fills=getattr(marked, "_fills", None),
        blocks=bool(getattr(marked, "_blocks", False)),
        instant=bool(getattr(marked, "_instant", False)),
        prop=isinstance(held, property),
        binds=getattr(marked, "_binds", ""),
        reads=getattr(marked, "_reads", ""),
        member_collection=getattr(marked, "_member_collection", ""),
        cxx_body=_body(node),
        local=bool(getattr(marked, "_local", False)),
        virtual=bool(getattr(marked, "_virtual", False)),
        posted=bool(getattr(marked, "_posted", False)),
        wire_read=getattr(marked, "_wire_read", ""),
        headers=tuple(getattr(marked, NEEDS, ())),
        spells=tuple(getattr(marked, "_spells", ())),
        startup=bool(getattr(marked, "_startup", False)),
        translator=bool(getattr(marked, "_translator", False)),
        policy=getattr(marked, "_policy", None),
        subscription=getattr(marked, "_subscription", None),
    )


def _members(node: ast.ClassDef) -> tuple[Member, ...]:
    """The words of a vocabulary, in the order it lists them.

    `NAME = "value"`, and the docstring that may follow it. Python
    puts an attribute's documentation under the assignment rather
    than inside it, so the two are read as one pair here."""
    out: list[Member] = []
    for i, item in enumerate(node.body):
        if not isinstance(item, ast.Assign):
            # The class's docstring, or a word's. Anything else would be
            # dropped by every stage that reads the words, silently.
            if (_is_docstring(item)
                    and (i == 0 or isinstance(node.body[i - 1], ast.Assign))):
                continue
            raise DeclarationError(
                item, f"{node.name}: a vocabulary holds words and their "
                      f"docstrings, and nothing else")
        if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
            raise DeclarationError(item, "a word is one plain assignment")
        if not (isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)):
            raise DeclarationError(
                item, "a word IS a string. Nothing else crosses as one.")
        doc = ""
        nxt = node.body[i + 1] if i + 1 < len(node.body) else None
        if (isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)):
            doc = nxt.value.value
        out.append(Member(targets_name(item), item.value.value, doc))
    if not out:
        raise DeclarationError(node, f"{node.name}: a vocabulary with no words")
    return tuple(out)


def _is_docstring(item: ast.stmt) -> bool:
    return (isinstance(item, ast.Expr) and isinstance(item.value, ast.Constant)
            and isinstance(item.value.value, str))


def targets_name(item: ast.Assign) -> str:
    target = item.targets[0]
    assert isinstance(target, ast.Name)
    return target.id


def _class(node: ast.ClassDef, vocab: dict[str, str],
           where: str, here: str,
           fns: dict[int, Any]) -> Class:
    # The import already ran this class's decorators, on the class
    # itself, so what they wrote is read off it rather than written
    # again onto a stand-in.
    _check_markers(node.decorator_list, "class")
    holder = fns.get(_first_line(node))
    if not isinstance(holder, type):
        raise DeclarationError(
            node, f"{node.name}: the import has no class at this line.")
    decl: Decl = holder.__dict__.get(DECL, Decl())
    decl.name = node.name
    # The base, said the way Python says it.
    #
    # This used to be `@derives("Store")` and a Python base class was
    # REFUSED, for a reason that has since expired: "a Python
    # hierarchy here would be one among objects that are never
    # constructed". Declarations are imported now, so the hierarchy is
    # real - `LocalFSStore` genuinely is a subclass of `Store`, a type
    # checker sees it, and the name is a use of the import rather than
    # a string that happens to match one.
    #
    # Found by Carl asking why it was ever an annotation, and by ruff
    # answering first: `@derives("Store")` left the import unused.
    # A vocabulary IS a StrEnum, in the declaration as in the emitted
    # module, so a default such as `HashAlgorithm.SHA256` types as the
    # word and not as `str` (huggorm#104). The base is Python's, not a
    # declared class, so it is checked here and never becomes `base`.
    if decl.kind is DeclKind.WORDS:
        if holder.__bases__ != (enum.StrEnum,):
            raise DeclarationError(
                node, f"{node.name}: a vocabulary is a StrEnum. Write "
                      f"`class {node.name}(StrEnum)`.")
    elif holder.__bases__ != (object,):
        # One base, and only on an @in_process class. Nix hands a
        # Python-implemented store back as the object Python made, so
        # that object must BE a Store to nanobind: accepted where one
        # is, with its methods (huggorm#149). The async and remote
        # surfaces carry no hierarchy, so a served class has no base.
        (base,) = holder.__bases__ if len(holder.__bases__) == 1 else (None,)
        if (base is None or "_decl" not in base.__dict__
                or decl.wire is not Crossing.LOCAL):
            raise DeclarationError(
                node, f"{node.name}: only an @in_process class has a base, "
                      f"and it is one declared class. Declare the methods "
                      f"on {node.name} itself (huggorm#60).")
        decl.base = base.__name__
    # `@needs` writes onto whatever it decorates, and on a class that
    # is the stand-in rather than the Decl - so it is read here
    # instead of being restated in declare.py, which is the same trick
    # every other decorator gets.
    decl.headers = tuple(holder.__dict__.get(NEEDS, ()))

    if decl.kind is DeclKind.WORDS:
        return Class(
            name=node.name,
            doc=ast.get_docstring(node, clean=False) or "",
            decl=decl,
            ctor=None,
            members=_members(node),
            module=where,
        )

    ctor: Method | None = None
    from_parts: Method | None = None
    methods: list[Method] = []
    # The IMPORT says which members exist, in definition order, with
    # any `NIX_VERSION` branch already chosen; the tree gives each
    # one's text, found by the line the import names.
    nodes = {_first_line(n): n for n in ast.walk(node)
             if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}
    items: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for name, value in vars(holder).items():
        fn = _written(value, here)
        if fn is None:
            if not _dunder(name) and name not in (DECL, NEEDS):
                _survive(DeclarationError(
                    _binder(node, name),
                    f"{node.name}.{name} ({type(value).__name__}) reaches "
                    f"no output. A bound class holds its methods, its "
                    f"constructor, {FROM_PARTS} and what its decorators "
                    f"wrote."), unsound=f"{node.name}.{name}")
            continue
        _one_definition(fn, nodes)
        items.append(nodes[fn.__code__.co_firstlineno])
    for item in items:
        if isinstance(item, ast.AsyncFunctionDef):
            # A declaration says what the BINDING is, and a binding is
            # C++. Which methods get an async form is decided from
            # `@binding(threading=...)` and `@blocks`, one file later,
            # so `async def` here says nothing the emitter can use.
            #
            # It was refused already, by accident and with the wrong
            # cause: the import keeps the function, so it named a live
            # line, and `_reconcile` saw a line the tree reader had no
            # node for. The message blamed `co_firstlineno` and never
            # said "async" (huggorm#88). `DEFINITIONS` closed that,
            # which is what makes this refusal reachable.
            _survive(DeclarationError(
                item,
                f"{node.name}.{item.name}: a declaration describes a C++ "
                f"binding, so `async def` says nothing here. The async "
                f"form is DERIVED - @binding(threading=...) and @blocks "
                f"decide which methods get one - so write a plain `def`."),
                unsound=f"{node.name}.{item.name}")
            continue
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name == FROM_PARTS:
            # Not a method. It takes the parts the wire carried, and
            # the emitter writes that signature from the field list -
            # so the declaration writes the BODY and nothing else. A
            # `self` here would be the object it exists to build.
            from_parts = _method(item, vocab, fns, bound=False)
            if from_parts.params:
                raise DeclarationError(
                    item,
                    f"{node.name}.{FROM_PARTS} takes no parameters here. "
                    f"The wire fields ARE its parameters, and the emitter "
                    f"writes them from the field list so the two cannot "
                    f"disagree. The body reads them by name.")
            continue
        if item.name == "__init__":
            ctor = _method(item, vocab, fns)
            if decl.factory and ctor.params:
                # A class with a factory is built by it, so it owns
                # the signature. Declaring it twice is how the
                # `uri="auto"` default died: `open_store` carried it,
                # `__init__` did not, and the emitter read the wrong
                # one - so `Store()` raised, `AsyncStore()` required
                # an argument, and the generated surfaces agreed with
                # each other about the wrong thing.
                #
                # The `__init__` is still worth writing: it is where
                # the PROSE goes, and a caller reading the declaration
                # looks for it under the name they will call. Only the
                # parameters are refused.
                raise DeclarationError(
                    item,
                    f"{node.name}.__init__ declares parameters, but "
                    f"@constructs({node.name}) says {decl.factory} "
                    f"builds one - so {decl.factory} owns the "
                    f"signature. Move them there and leave the "
                    f"docstring here.")
        elif item.name.startswith("__") and item.name not in DECLARED_DUNDERS:
            # Without this refusal, a SILENT SKIP. The loop would keep
            # the names that are not `__`-prefixed and drop the rest
            # with no answer, so a declaration that writes
            # `def __call__` would get no binding, no stub line, no rpc
            # and no diagnostic - and a skip is
            # indistinguishable from an absence, which is this repo's
            # named failure mode (huggorm#88).
            #
            # `__init__` is the one exception and it is handled above.
            # The value dunders are DERIVED rather than declared:
            # `@wire_value` says which ones a class owes - its
            # `order=` and `text=` are what decide - and the
            # emitter writes them, so a
            # declaration that writes `__repr__` is restating a fact
            # its own decorator already carries.
            #
            # A name that starts with `__` and does not end with one
            # lands here too, and the answer is the same: Python
            # mangles it, nothing reads it, and the reader says so
            # rather than dropping it.
            _survive(DeclarationError(
                item,
                f"{node.name}.{item.name}: a name starting with `__` "
                f"reaches no emitter unless the emitters are taught it. "
                f"`__init__` and {sorted(DECLARED_DUNDERS)} are. The "
                f"value dunders are derived from the class decorators - "
                f"@wire_value writes them, and its `order=` and "
                f"`text=` decide which - so do not declare one. "
                f"Anything else needs the emitters taught (huggorm#88); "
                f"declare it under a plain name until then."),
                unsound=f"{node.name}.{item.name}")
        else:
            # Definition order, which is the order a reader of the
            # declaration sees and the order the emitted file keeps.
            #
            # SIBLINGS, like the classes in `read`. Two mistakes in
            # one class are two mistakes, and stopping at the first
            # made a class as coarse a boundary as a file. Nothing
            # cross-checks methods BETWEEN classes, so a class short
            # one method produces no bogus diagnostic - the collector
            # refuses the whole read before any emitter sees it.
            try:
                methods.append(_method(item, vocab, fns))
            except DeclarationError as e:
                _survive(e, unsound=f"{node.name}.{item.name}")
    out = Class(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        decl=decl,
        ctor=ctor,
        methods=tuple(methods),
        module=where,
        from_parts=from_parts,
    )
    if from_parts is not None:
        _mentions(out, node)
    return out



def _mentions(cls: Class, node: ast.AST) -> None:
    """Every declared wire field must appear in the body, by name.

    Crude, and it buys the one thing the synthetic struct gave away.
    A record's `_from_parts` was aggregate initialisation, so a
    missing field could not compile. A hand-written one that never
    assigns `ultimate` COMPILES and zero-initialises it, and the value
    then crosses the wire losing a field in silence.

    This does not prove the body USES a field correctly - only the
    round-trip test can, and it does (huggorm#56). It catches the
    forgetting, which is the failure that costs nothing to catch here
    and a debugging session to catch there.

    A false positive costs one comment naming the field."""
    assert cls.from_parts is not None
    body = cls.from_parts.cxx_body
    seen = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                          body.text if body is not None else ""))
    for f, _ in cls.parts:
        if f.name not in seen:
            raise DeclarationError(
                node,
                f"{cls.name}.{FROM_PARTS} never mentions '{f.name}', which "
                f"crosses the wire. A body that drops a part compiles and "
                f"loses it in silence.\n"
                f"An accessor joins the wire by existing, so a NEW one "
                f"lands here: either consume it in {FROM_PARTS}, or mark "
                f"it @local - a value this side computes and does not "
                f"send.")


def _guarded_import(name: str, globals: Mapping[str, object] | None = None,
                    locals: Mapping[str, object] | None = None,
                    fromlist: Sequence[str] = (), level: int = 0) -> ModuleType:
    """`__import__` for a declaration's own statements.

    A declaration describes C++; it computes nothing, so it needs
    nothing past the vocabulary, the other declarations and the
    typing names. Not a sandbox: declarations are trusted repository
    code, and this catches a mistake (huggorm#123)."""
    if level or not any(name == m or name.startswith(f"{m}.")
                        for m in IMPORTABLE):
        raise ImportError(
            f"a declaration imports only from {', '.join(IMPORTABLE)}, "
            f"not {'.' * level}{name}")
    return builtins.__import__(name, globals, locals, fromlist, level)


def _guarded(module: ModuleType) -> None:
    """Scope the import guard to one declaration's own statements.

    The modules it imports run under the real builtins, and a
    declaration among them is guarded when it runs itself."""
    module.__builtins__ = {**vars(builtins), "__import__": _guarded_import}  # type: ignore[attr-defined]


class _DeclarationLoader(importlib.machinery.SourceFileLoader):
    def exec_module(self, module: ModuleType) -> None:
        _guarded(module)
        super().exec_module(module)


class _DeclarationFinder(importlib.abc.MetaPathFinder):
    """Owns every `huggorm_decl.decl.*` import, so a declaration is one
    module object however it is reached: by `load`, by another
    declaration or by a test. Two objects made `StorePath` two classes
    (huggorm#140)."""

    def find_spec(self, fullname: str, path: Sequence[str] | None,
                  target: ModuleType | None = None
                  ) -> importlib.machinery.ModuleSpec | None:
        if not fullname.startswith(f"{DECLARATIONS}."):
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.origin is None or not isinstance(
                spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        # Resolved: a Nix Python env reaches the package through a
        # symlink, and `_definitions` matches `co_filename` against the
        # resolved path. Unresolved, every definition went unmatched.
        origin = str(pathlib.Path(spec.origin).resolve())
        return importlib.util.spec_from_file_location(
            fullname, origin, loader=_DeclarationLoader(fullname, origin))


if not any(isinstance(f, _DeclarationFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _DeclarationFinder())


def _declaration_name(path: pathlib.Path) -> str | None:
    """The dotted name a file in the declarations package imports as,
    or None for a file outside it, such as a test's."""
    try:
        package = importlib.util.find_spec(DECLARATIONS)
    except ModuleNotFoundError:
        return None
    if package is None or package.submodule_search_locations is None:
        return None
    homes = {pathlib.Path(p).resolve()
             for p in package.submodule_search_locations}
    return f"{DECLARATIONS}.{path.stem}" if path.parent in homes else None


@functools.cache
def load(path: str) -> ModuleType:
    """One declaration, IMPORTED.

    A declaration is read twice, and this is the second reading. The
    tree says how to render a definition; the import says which
    definitions exist, because a declaration may branch on
    `NIX_VERSION` and Python is what resolves that.

    The module is handed back rather than consumed. `_live` wanted
    only a set of line numbers and threw the rest away, which threw
    away the thing an import is uniquely good for: INHERITANCE.
    Python computed every base chain and every inherited attribute
    while executing the file, and a reader that keeps the module gets
    all of it for free instead of walking the tree again.

    A file that will not import is REFUSED, with the reason it gave.
    This used to answer None, on the reading that a declaration is a
    document first and one that cannot be imported still parses - so
    a caller fell back to the tree alone.

    That fallback is silent, and it is silent in the way this repo
    has now been bitten by four times. A `@property` under any other
    marker raises `AttributeError: 'property' object has no attribute
    '_instant'` while the module executes, and the file then read
    tree-only - as did every file importing from it. The tree read is
    not obviously wrong: a declaration with no `NIX_VERSION` branch
    keeps exactly the same nodes either way, so nothing anywhere
    said a word (huggorm#82).

    Nothing wanted the fallback. Every declaration in `decl/`
    imports, and the one emitter that already asked - `pyerrors` -
    refused a non-importing file rather than guessing at a hierarchy
    it could not read. This says the same thing for all of them, and
    says WHY, which that refusal could not.

    CACHED by path, because two readers now want the same module and
    executing a declaration twice would run its decorators twice."""
    here = pathlib.Path(path).name
    real = _declaration_name(pathlib.Path(path).resolve())
    try:
        if real is not None:
            return importlib.import_module(real)
        spec = importlib.util.spec_from_file_location(
            f"_huggorm_decl_{pathlib.Path(path).stem}", path)
        if spec is None or spec.loader is None:
            raise DeclarationError.already(
                f"{here}: Python will not load this file as a module at "
                f"all.")
        mod = importlib.util.module_from_spec(spec)
        _guarded(mod)
        spec.loader.exec_module(mod)
    except DeclarationError:
        raise
    except Exception as e:
        # The cause, verbatim. A declaration fails to import for
        # reasons that are one line to fix and impossible to guess
        # at: a marker decorator over a `@property` sets an attribute
        # on a descriptor and raises, and "it did not import" alone
        # would send a reader to the wrong file.
        raise DeclarationError.already(
            f"{here}: the declaration does not import, so nothing says "
            f"which definitions exist. {type(e).__name__}: {e}"
            f"{_descriptor_hint(e)}") from e
    return mod


# A marker decorator applied to a `property`. Python's own message
# names the descriptor and the attribute and stops there, which says
# WHAT broke and not what to do about it.
#
# `property` ONLY, and the first version of this named `staticmethod`
# and `classmethod` beside it. Measured on 3.14.7, and they do not
# belong:
#
#     property       REFUSES - no __dict__ for setting new attributes
#     staticmethod   accepts an attribute
#     classmethod    accepts an attribute
#
# So `@instant` over a `@staticmethod` IMPORTS, and the declaration
# reaches `_method`'s own refusal instead - which already names the
# real problem, that a static method's first parameter would be
# dropped. The two arms were text that could never run, claiming a
# coverage this hint does not have (huggorm#75).
_ON_DESCRIPTOR = re.compile(
    r"'(property)' object has no attribute '_(\w+)'")


def _descriptor_hint(exc: BaseException) -> str:
    """The one-line fix, when the import failed for the known reason.

    `@property` and a marker on one accessor is legal, and the ORDER
    decides whether it imports. Measured both ways (huggorm#76):

        @instant                  AttributeError, at import
        @property                 - a property takes no attribute
        def base16(self): ...

        @property                 prop=True instant=True
        @instant                  - the marker gets the function
        def base16(self): ...

    So `@property` OUTERMOST is the answer, and it costs the reader
    nothing: the reader takes the function out of the property
    (`fget`) and reads the marker off it, and `prop` is read from the
    tree.

    The MARKER is derived rather than listed. Every marker in
    `declare.py` writes `_<name>` onto what it is handed, so the
    shape is the fact and a list would be that fact stated twice and
    would go stale on the next marker.

    The DESCRIPTOR is named, and only one of them is: `property` is
    the only builtin descriptor that refuses an attribute. That is
    measured, and the regex comment holds the measurement.

    huggorm#76 asked for this to be said in a REFUSAL rather than in
    a comment, because a comment is not where a reader who hit it is
    looking. The comment above is now the second copy, and the
    refusal is the one that reaches them.
    """
    m = _ON_DESCRIPTOR.search(str(exc))
    if m is None:
        return ""
    descriptor, marker = m.group(1), m.group(2)
    # HONEST ABOUT WHAT THE ORDER BUYS. It fixes the IMPORT and
    # nothing else: the emitter still refuses a `@property` accessor
    # (huggorm#76). A hint that stopped at the order would send a
    # reader to a second refusal with no warning that it was coming,
    # which is worse than the raw AttributeError it replaces.
    return (f"\nWrite @{descriptor} OUTERMOST, above @{marker}. A marker "
            f"sets `_{marker}` on what it is handed, and a {descriptor} "
            f"object takes no attribute - so the marker has to reach the "
            f"function underneath it. That fixes the IMPORT. An emitter "
            f"still refuses a @{descriptor} accessor - see huggorm#76 - so "
            f"declare it as a plain method until that changes.")


def _live(path: str) -> set[int]:
    """The first line of every class and function the IMPORT kept.

    A declaration may branch on `NIX_VERSION`, and Python resolves
    that during the import. This asks the resulting module which
    definitions survived, and the answer is a set of LINES.

    Lines, not names: both arms of an `if` define the same name, so a
    name match keeps both and the emitter binds the method twice.
    `co_firstlineno` is the first DECORATOR's line when a definition
    has decorators and the `def`/`class` line when it has none, so a
    caller matches either.

    Always a set. `load` refuses a file that will not import, so
    "the import kept nothing because there was no import" is no
    longer one of the answers (huggorm#82)."""
    # `_definitions`' walk, so the two cannot disagree about what a
    # definition is.
    return set(_definitions(path))


def _one_definition(fn: Any, nodes: dict[int, Any]) -> None:
    """Refuse a name that `typing.@overload` gives several definitions.

    Every surface binds one definition per name: the stub, the async
    layer and the RPC route. Asked by the import, not by the decorator
    spelling, so `@typing.overload` is refused too."""
    if earlier := get_overloads(fn):
        raise DeclarationError(
            nodes[earlier[0].__code__.co_firstlineno],
            f"{fn.__name__} is overloaded. A binding holds one definition "
            f"per name; give each signature its own name.")


def _definitions(path: str) -> dict[int, Any]:
    """Every class and function the import kept, by its first line.

    The same walk `_live` makes, and the same key: `co_firstlineno` is
    the first decorator's line, so a tree node finds its function by
    `_first_line(node)`. The import is where an annotation becomes an
    object, so this is how the reader reaches one.

    `@overload` replaces a name with its last definition, and the
    earlier ones are only in `typing.get_overloads`, so those are
    walked too."""
    mod = load(path)
    here = str(pathlib.Path(path).resolve())
    out: dict[int, Any] = {}

    def note(obj: object) -> None:
        # A DESCRIPTOR holds the function rather than being one, so
        # the function is asked for it. Without this a `@property`
        # accessor named no line and vanished from the binding in
        # silence (huggorm#75).
        fn = getattr(obj, "fget", None) or getattr(obj, "__func__", obj)
        if not isinstance(fn, types.FunctionType):
            return
        # Python 3.14 compiles a module's annotations into an
        # `__annotate__` function at line 1. No one wrote it.
        if fn.__code__.co_filename != here or fn.__name__ == "__annotate__":
            return
        out[fn.__code__.co_firstlineno] = obj
        for one in get_overloads(fn):
            if isinstance(one, types.FunctionType):
                out[one.__code__.co_firstlineno] = one

    for obj in vars(mod).values():
        note(obj)
        if isinstance(obj, type) and obj.__module__ == mod.__name__:
            # `__firstlineno__` is the first decorator's line, as
            # `co_firstlineno` is for a function.
            out[obj.__firstlineno__] = obj
            for member in vars(obj).values():
                note(member)
    return out


def _first_line(node: ast.FunctionDef | ast.AsyncFunctionDef
                | ast.ClassDef) -> int:
    """The line `co_firstlineno` names for this definition."""
    return node.decorator_list[0].lineno if node.decorator_list else node.lineno


def _reconcile(tree: ast.Module, live: set[int], path: str) -> None:
    """Every definition the import kept must exist in the tree.

    The two readings come from one file in one call, so they cannot
    drift the way two declarations of one fact drift - that was F1 and
    F7a. What they CAN do is stop lining up, and the way that happens
    is a Python release changing what `co_firstlineno` points at.

    Then nothing matches, `_resolve` keeps nothing, and the module
    emits an empty binding. Silently. This is the check that makes it
    loud.

    The other direction, `nodes - live`, holds every dead `if` arm, so
    it is no error in general. A definition under NO `if` is: the
    import ran it, so a line the import did not keep means the object
    went somewhere `_definitions` does not look - a descriptor
    (huggorm#75), a rebound name, a wrapping decorator. The reader
    would drop it in silence (huggorm#140)."""
    if not live:
        return
    unbranched = [n for n in tree.body if isinstance(n, DEFINITIONS)]
    unbranched += [m for n in unbranched if isinstance(n, ast.ClassDef)
                   for m in n.body if isinstance(m, DEFINITIONS)]
    for n in unbranched:
        if not ({n.lineno} | {d.lineno for d in n.decorator_list}) & live:
            raise DeclarationError(
                n, f"{n.name}: this definition is under no `if`, and the "
                   f"import kept nothing at its line. Something replaced "
                   f"or wrapped it, and the reader would drop it.")
    nodes = {n.lineno for n in ast.walk(tree)
             if isinstance(n, DEFINITIONS)}
    nodes |= {d.lineno for n in ast.walk(tree)
              if isinstance(n, DEFINITIONS)
              for d in n.decorator_list}
    orphans = sorted(live - nodes)
    if orphans:
        raise DeclarationError(
            tree,
            f"{pathlib.Path(path).name}: the import kept definitions at "
            f"lines {orphans} and the tree has no node there. The two "
            f"readings have stopped lining up - most likely "
            f"`co_firstlineno` no longer points where this assumes.")


def _resolve(body: list[ast.stmt], live: set[int]) -> list[ast.stmt]:
    """One body with its `if` arms already chosen.

    Nothing here evaluates a condition. Python did that during the
    import, and this keeps what Python kept - matched by the line a
    definition starts on.

    There used to be a third answer here: with `live` unknown the
    `if` was flattened whole, both arms read, the name usually
    declared twice. It was defended as loud rather than silent, and
    it was neither - a declaration with no `if` in it keeps exactly
    the same nodes that way, so a file that did not import read as a
    file that did. `load` refuses one now, so `live` is always known
    (huggorm#82)."""
    out: list[ast.stmt] = []

    def walk(nodes: list[ast.stmt]) -> None:
        for n in nodes:
            if isinstance(n, ast.If):
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, DEFINITIONS):
                own = {n.lineno} | {d.lineno for d in n.decorator_list}
                if own & live:
                    out.append(n)
            else:
                out.append(n)

    walk(body)
    return out


@functools.cache
def read(path: str) -> Module:
    """One declaration file, read twice.

    The import holds the facts; the tree supplies the text. No fact
    is taken from both.

    Cached by path, as `load` is. Every declaration that imports
    another reads it again, and one corpus read made 502 reads of
    about 20 files, 137 of them `words.py`."""
    with reading(path):
        return _read(path)


def resolved(path: str) -> ast.Module:
    """One declaration's tree, with its version branches already chosen.

    The reading `Corpus.tree` cannot give. A raw parse holds BOTH arms
    of an `if NIX_VERSION >= ...`, so an emitter that walks it either
    reads a class this build does not have or - worse - reads
    `tree.body` and misses one it does. `read()` has resolved this
    since it was written; this hands the same answer to an emitter
    that wants the tree rather than a `Module`.

    Flat, and that is the point rather than a side effect. The `if`
    is GONE from the body: Python chose an arm during the import, so
    a branch is a build-time question and the emitted output is what
    this build links. An emitter downstream never sees a condition
    and never has to evaluate one.

    Written for `pyerrors`, which reads the tree directly because an
    exception declaration IS its own output. It read `tree.body` and
    so saw neither arm of a branch, while the module transform copied
    both through - three holes at once, and none of them loud
    (huggorm#73)."""
    with reading(path):
        return _chosen(path)[0]


def _chosen(path: str) -> tuple[ast.Module, set[int]]:
    """One declaration parsed, its `if` arms chosen, and which lines
    the import kept.

    The four lines both readings open with. `resolved` would have
    been a copy of them - same parse, same `_live`, same reconcile,
    same `_resolve` - and the fact that copy would have restated is
    which arm of a branch this build is looking at."""
    tree = ast.parse(pathlib.Path(path).read_text(), filename=path)
    live = _live(path)
    _reconcile(tree, live, path)
    return ast.Module(body=_resolve(tree.body, live), type_ignores=[]), live


def _read(path: str) -> Module:
    """`read` with the path already on the stack.

    The IMPORT drives: `vars` holds every name the build has, in
    definition order, with each `NIX_VERSION` branch already chosen.
    The tree supplies each definition's text and position, found by
    the line the import names. A name the reader can put nowhere is
    refused, so nothing a declaration writes reaches no output in
    silence (huggorm#73, #75, #78, #88, #123).

    A module holds declared classes, exceptions, union aliases,
    decorated functions and imports. IMPORTS are read from the tree:
    `Str` and a union this file declares are both a `typing` alias,
    and `NIX_2_36` is a bare bool, so the object cannot say where it
    came from."""
    tree = ast.parse(pathlib.Path(path).read_text(), filename=path)
    mod = load(path)
    fns = _definitions(path)
    _reconcile(tree, set(fns), path)
    nodes = {_first_line(n): n for n in ast.walk(tree)
             if isinstance(n, DEFINITIONS)}
    assigned = _assignments(tree)
    vocab = _vocabulary(tree)
    stem = pathlib.Path(path).stem
    here = str(pathlib.Path(path).resolve())
    imported = {a.asname or a.name.split(".")[0]
                for n in ast.walk(tree)
                if isinstance(n, ast.Import | ast.ImportFrom)
                for a in n.names}
    glb = vars(mod)
    # SIBLINGS, so one bad definition does not hide the next. Each is
    # read in full or not at all - the failure is recorded, its name
    # is marked unsound, and the loop goes on.
    classes: list[Class] = []
    errors: list[Class] = []
    functions: list[Method] = []
    unions: list[Class] = []
    for name, value in glb.items():
        if _dunder(name) or name in imported:
            continue
        fn = _written(value, here)
        try:
            if isinstance(value, type) and value.__module__ == mod.__name__:
                node = nodes[value.__firstlineno__]
                assert isinstance(node, ast.ClassDef)
                if node.decorator_list:
                    cls = _class(node, vocab, stem, here, fns)
                    classes.append(cls)
                else:
                    errors.append(_error(node, value, glb, stem))
            elif fn is not None:
                _one_definition(fn, nodes)
                functions.append(_free(nodes[fn.__code__.co_firstlineno],
                                       vocab, fns))
            elif _union(value) is not None and name in assigned:
                unions.append(_union_class(name, value, stem,
                                           *assigned[name]))
            else:
                raise DeclarationError(
                    assigned.get(name, (tree, None))[0],
                    f"{name} ({type(value).__name__}) reaches no output. A "
                    f"declaration holds declared classes, exceptions, union "
                    f"aliases, decorated functions and imports.")
        except DeclarationError as e:
            _survive(e, unsound=name)
    uses = _uses(tree, pathlib.Path(path).parent)
    # ...checked once the arms can be resolved, which needs the
    # imports this file made and the classes it declares itself.
    #
    # THE ONE CROSS-CLASS CHECK IN THIS READER, which is why bogus
    # errors have one source and one cure. Every other refusal above
    # is about a single class or this file's own vocabulary.
    resolvable = {**uses, **{c.name: c for c in classes},
                  **{u.name: u for u in unions}}
    for u in unions:
        try:
            _check_arms(u, resolvable, assigned[u.name][0])
        except DeclarationError as e:
            _survive(e, unsound=u.name)
    return Module(
        name=stem,
        doc=ast.get_docstring(tree, clean=False) or "",
        classes=tuple(classes),
        errors=tuple(errors),
        functions=tuple(functions),
        vocabulary=vocab,
        unions=tuple(unions),
        uses=uses,
    )


def _free(node: DefinitionNode, vocab: dict[str, str],
          fns: dict[int, Any]) -> Method:
    """One module-level function: a FREE binding, if it is decorated.

    nanopynix has 72 of them, `m.def("open_store", &open_store_uri,
    "uri"_a)` and its kind. An undecorated one is refused: nothing
    reads it."""
    if isinstance(node, ast.ClassDef) or not node.decorator_list:
        raise DeclarationError(
            node, f"{node.name} (function) reaches no output. A free "
                  f"binding is decorated; a declaration holds no helpers.")
    if isinstance(node, ast.AsyncFunctionDef):
        # As in a class body, and for the same reason.
        raise DeclarationError(
            node,
            f"{node.name}: a declaration describes a C++ binding, so "
            f"`async def` says nothing here. The async form is "
            f"DERIVED from @threading - so write a plain `def`.")
    return _method(node, vocab, fns, bound=False, bound_kind=False)


def _assignments(tree: ast.Module) -> dict[str, tuple[ast.Assign, str]]:
    """Each plain `NAME = ...` the file writes, with the docstring under it.

    Every statement list, so an assignment in either arm of an `if` is
    found; the import says which arm ran. Python puts an attribute's
    documentation under the assignment rather than inside it, so the
    two are read as one pair."""
    out: dict[str, tuple[ast.Assign, str]] = {}
    for parent in ast.walk(tree):
        for body in (getattr(parent, "body", None),
                     getattr(parent, "orelse", None)):
            if not isinstance(body, list) or isinstance(parent, ast.ClassDef):
                continue
            for i, item in enumerate(body):
                if not (isinstance(item, ast.Assign) and len(item.targets) == 1
                        and isinstance(item.targets[0], ast.Name)):
                    continue
                nxt = body[i + 1] if i + 1 < len(body) else None
                doc = (nxt.value.value if nxt is not None and _is_docstring(nxt)
                       else "")
                out[item.targets[0].id] = (item, doc)
    return out


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _written(value: object, here: str) -> types.FunctionType | None:
    """The function a declaration wrote, under any descriptor."""
    fn = getattr(value, "fget", None) or getattr(value, "__func__", value)
    if not isinstance(fn, types.FunctionType):
        return None
    # Python 3.14 compiles annotations into an `__annotate__` function.
    # No one wrote it.
    if fn.__code__.co_filename != here or fn.__name__ == "__annotate__":
        return None
    return fn


def _binder(tree: ast.AST, name: str) -> ast.AST:
    """The statement that binds `name`, so a refusal names its line."""
    for node in ast.walk(tree):
        if isinstance(node, DEFINITIONS) and node.name == name:
            return node
        if isinstance(node, ast.Assign | ast.AnnAssign | ast.AugAssign):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            if any(isinstance(t, ast.Name) and t.id == name
                   for t in targets):
                return node
    return tree


def _union(alias: object) -> tuple[object, list[object]] | None:
    """The union an alias holds and its `Annotated` metadata, or None."""
    held, meta = alias, list[object]()
    if get_origin(alias) is Annotated:
        held, *meta = get_args(alias)
    if get_origin(held) not in (types.UnionType, typing.Union):
        return None
    return held, meta


def _union_class(name: str, alias: object, where: str,
                 item: ast.Assign, doc: str) -> Class:
    """One module-level union alias, as a Class the emitters can name.

    A union is written as the alias it is:

        DerivedPath = StorePath | DerivedPathBuilt

    Real Python, so a reader of the file and a type checker both see a
    union; and the alias NAME is what the wire calls the message, so
    nothing has to invent one.

    It comes back as a `Class` because that is what lets every emitter
    resolve it through `known` - the same way a vocabulary does -
    rather than learning a second kind of thing."""
    found = _union(alias)
    assert found is not None
    held, meta = found
    parts = [(_scalar_arm(a, name, item), a) for a in get_args(held)]
    scalars = {s.wire: s for s, _ in parts if s is not None}
    arms = tuple(s.wire if s is not None
                 else "None" if a is type(None)
                 else getattr(a, "__name__", repr(a))
                 for s, a in parts)
    variant = _variant(name, tuple(meta), arms, item)
    return Class(
        name=name, doc=doc, ctor=None, module=where,
        decl=Decl(name=name, kind=DeclKind.UNION, arms=arms, wire=Crossing.VALUE,
                  variant=variant, scalars=scalars),
    )


def _scalar_arm(arm: object, union: str, node: ast.AST) -> Type | None:
    """A union arm that crosses as a builtin, or None for a class arm.

    Named by its WIRE spelling, `uint` for a U64, because that is the
    spelling that carries the width. Above the boundary it is an `int`
    like any other."""
    if isinstance(arm, type) and arm.__module__ == "builtins" \
            and arm is not type(None):
        raise DeclarationError(
            node, f"{union}: '{arm.__name__}' names no C++ type. Use the "
                  f"alias - Str, I64, U64, Bint - so the arm has one.")
    if get_origin(arm) is not Annotated:
        return None
    held, *meta = get_args(arm)
    cxx = next((m for m in meta if isinstance(m, Cxx)), None)
    if cxx is None:
        return None
    return _undeclared(held, cxx)


def _variant(name: str, meta: tuple[object, ...], arms: tuple[str, ...],
             node: ast.AST) -> declare.Variant | None:
    """The `Variant(...)` on a union's alias, checked against its arms.

    None when the alias carries none, which stays legal: a union
    whose C++ variant holds its arms as themselves needs no fact
    stated, and the emitter refuses only where it actually needs one.

    A `wraps` key that names no arm is refused here. It is the
    mistake a rename makes - the arm moves, the wrap does not - and
    it would otherwise emit a conversion for a type the variant does
    not hold."""
    variants = [m for m in meta if isinstance(m, declare.Variant)]
    if len(variants) > 1:
        raise DeclarationError(
            node, f"{name}: one Variant(...) on an alias. Two would be two "
                  f"answers to what one C++ union is.")
    if not variants:
        return None
    variant = variants[0]
    for arm in variant.wraps:
        if arm not in arms:
            raise DeclarationError(
                node, f"{name}: wraps names '{arm}', which is not an arm of "
                      f"this union. A wrap says how the C++ variant holds a "
                      f"DECLARED arm, so it can only name one of "
                      f"{', '.join(arms)}.")
    return variant


# What a union's ARM may be. Each refusal is its own sentence, because
# each is a different mistake.
def _check_arms(cls: Class, known: dict[str, Class], node: ast.AST) -> None:
    """The widened union rule, enforced where the arms can be resolved.

    `T | None` was the only union an annotation could hold, and it
    stayed narrow on purpose. This widens it exactly as far as a sum
    type needs and no further."""
    # A SCALAR arm is told apart from its siblings by its Python type,
    # so two arms of one Python type could not be. `uint` and `int`
    # are both an `int` above the boundary.
    seen: dict[str, str] = {}
    for arm, scalar in cls.decl.scalars.items():
        if (first := seen.setdefault(scalar.python, arm)) != arm:
            raise DeclarationError(
                node, f"{cls.name}: '{first}' and '{arm}' are both a "
                      f"Python {scalar.python}, so a value could not say "
                      f"which arm it is.")
    for arm in cls.decl.arms:
        if arm in cls.decl.scalars:
            continue
        other = known.get(arm)
        if other is None:
            # UNKNOWN, not wrong. The arm names a class this reader
            # failed to read, or one an import did not deliver, so
            # whether it is a legal arm is a question nothing here can
            # answer - and answering it anyway is the bogus message.
            # A union with one unsound arm and one genuinely bad arm
            # still reports the bad one, because this skips the arm
            # rather than the union.
            if _COLLECTING is not None and (
                    arm in _COLLECTING.unsound or _COLLECTING.blind):
                continue
            raise DeclarationError(
                node, f"{cls.name}: '{arm}' is not a declared class. A "
                      f"union names types some declaration declares, or "
                      f"a scalar alias such as Str, U64 or Bint.")
        if other.is_words:
            raise DeclarationError(
                node, f"{cls.name}: '{arm}' is a vocabulary, which crosses "
                      f"as a plain string - so an arm of it is "
                      f"indistinguishable from any other string arm.")
        if other.is_union:
            raise DeclarationError(
                node, f"{cls.name}: '{arm}' is itself a union. Flatten it: "
                      f"a oneof of a oneof is one oneof, and nesting them "
                      f"hides which arms exist.")
        if other.decl.wire is not Crossing.VALUE:
            raise DeclarationError(
                node, f"{cls.name}: '{arm}' crosses as a {other.decl.wire}, "
                      f"not as a value. An arm that granted a lease would "
                      f"make every union a bulk-lease problem (huggorm#31).")


def _error(node: ast.ClassDef, kls: type, home: dict[str, Any],
           stem: str) -> Class:
    """One EXCEPTION class this file declares.

    An error declaration wears no decorator - `cxx = "nix::Error"` is
    a bare assignment, because there is no behaviour to mark. So an
    undecorated class IS an exception, and one that derives from none
    is refused: the module transform would copy it through, and no
    model entry and no catch clause would know it.

    The IMPORT says what the class derives from: `Interrupted` is a
    BaseException, as upstream's is, and a match on base names
    skipped it."""
    if not issubclass(kls, BaseException):
        raise DeclarationError(
            node, f"{node.name}: a class with no decorator declares "
                  f"an exception, so it derives from BaseException.")
    decl = Decl()
    decl.name = node.name
    decl.kind = DeclKind.ERROR
    return Class(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        decl=decl,
        ctor=None,
        module=stem,
        raised=_raised(node, kls, home),
    )


def _assigned(node: ast.ClassDef, kls: type, name: str) -> str:
    """The class's OWN `name = "..."`, or empty.

    `vars`, not `getattr`: a subclass inherits its base's catch, and
    an inherited `cxx` would emit a second catch of the base type."""
    value = vars(kls).get(name, "")
    if not isinstance(value, str):
        raise DeclarationError(
            node, f"{node.name}: `{name}` is {type(value).__name__}; it "
                  f"names C++, so write a string.")
    return value


def _raised(node: ast.ClassDef, kls: type, home: dict[str, Any]) -> Raised:
    """One exception's C++ facts, refused unless it can be caught right.

    All of it from the IMPORT. `cxx` and `header` belong to the class
    that writes them, so they come from its own `vars`. The bases,
    `_wire_fields` and `reader` are inherited through the MRO.

    `cxx` and `header` come as a pair. A `cxx` with no `header` is a
    catch whose type the emitted file reaches only through somebody
    else's include (huggorm#90). A `header` with no `cxx` reaches no
    emitter, and a line nobody reads looks exactly like one nobody
    wrote.

    A caught class with parts past the message needs a `reader`: its
    catch would call the constructor with parts missing, and
    `raise_as` turns that refusal into a RuntimeError, in silence."""
    cxx, header = _assigned(node, kls, CXX), _assigned(node, kls, HEADER)
    if cxx and not header:
        raise DeclarationError(
            node, f"{node.name}: `cxx = \"{cxx}\"` says the emitted "
                  f"translator catches this type, and nothing says "
                  f"which header declares it. Add `header = \"nix/...\"` "
                  f"beside it, or the emitted file reaches the type "
                  f"only through somebody else's include (huggorm#90).")
    if header and not cxx:
        raise DeclarationError(
            node, f"{node.name}: `header` with no `cxx`. Only a class "
                  f"the translator CATCHES needs a header emitted for "
                  f"it, so this line reaches no emitter - and a line "
                  f"nobody reads looks exactly like one nobody wrote.")
    fields = tuple(getattr(kls, WIRE_FIELDS, ()))
    reader = getattr(kls, READER, "")
    extra = fields[MESSAGE_PARTS:]
    if cxx and extra and not reader:
        raise DeclarationError(
            node, f"{node.name}: `_wire_fields` declares "
                  f"{[f for f, _ in extra]} beyond the message, and no "
                  f"`reader = \"...\"` says which C++ reads them off the "
                  f"caught exception.")
    here = home["__name__"]
    return Raised(
        bases=tuple(b.__name__ for b in kls.__bases__
                    if b.__module__ == here),
        parts=tuple((part, type_of(t, node, home)) for part, t in fields),
        cxx=cxx, header=header, reader=reader)


def _uses(tree: ast.Module, here: pathlib.Path) -> dict[str, Class]:
    """Declarations this one imported, read.

    `from huggorm_decl.decl.words import ContentAddressMethod`
    is how a declaration names a type another declaration owns. The
    import is never executed - nothing here is - but it is the one
    place that says WHICH other file to read, so following it beats
    every emitter holding a list of declarations to try."""
    out: dict[str, Class] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").startswith(f"{DECLARATIONS}."):
            continue
        stem = (node.module or "").rsplit(".", 1)[-1]
        source = here / f"{stem}.py"
        if not source.exists():
            _survive(DeclarationError(
                node, f"no declaration at {source}. A declaration may only "
                      f"import another declaration beside it."))
            _blinded()
            continue
        try:
            other = read(str(source))
        except DeclarationError as e:
            # The imported file refused. Its own diagnostics are
            # already recorded by the boundaries inside it; this is
            # the case where nothing was collecting, or where the
            # refusal was file-level.
            _survive(e)
            _blinded()
            continue
        # What the imported file itself imported, FIRST, so its own
        # classes win where a name appears twice.
        #
        # One level, and it is a union's arms that need it. A union is
        # `A | B` over classes that may live in a third file:
        # `DerivedPath` is declared in `derived_path.py` and one arm
        # of it is the `StorePath` that file imported from `path.py`.
        # Taking the union without its arms gives a module that can
        # NAME the union and cannot spell it - which failed as
        # `KeyError: 'StorePath'` inside the emitter, a long way from
        # the import that caused it.
        #
        # Not recursive. A second level would be a file naming a type
        # nothing in its own imports mentions, and there is no case
        # for it; the day there is, it is this line and a cycle guard.
        out.update(other.uses)
        for cls in (*other.classes, *other.unions, *other.errors):
            out[cls.name] = cls
    return out


def _blinded() -> None:
    """Say that an import did not read, so this file knows less.

    Without it the arm check below would report every type the missing
    declaration owned as "not a declared class" - which is true of
    what this reader can see and false about the tree, and is exactly
    the bogus message collection has to avoid."""
    if _COLLECTING is not None:
        _COLLECTING.blind = True
