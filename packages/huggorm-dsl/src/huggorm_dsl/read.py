"""
A declaration file, read TWICE: imported, and parsed.

The IMPORT is the authority on WHAT exists. A declaration may branch
on `NIX_VERSION`, and Python resolves that during the import - so
nothing here interprets a version condition, because the interpreter
is already present and is better at it than we would be.

The TREE is the source for HOW to render it. `ast.parse` keeps what
the import throws away: the C++ inside a body, the exact text of a
docstring, the order a class declares its methods in.

`_reconcile` holds the two together. Every definition the import kept
must exist in the tree, and a definition the tree has that the import
dropped is an `if` arm this Nix version does not take.

A body still never runs: `def` defines, it does not call. So `Cxx(...)`
in one is dead text this module lifts out of the tree, which is why a
declaration can name a C++ type this machine has never compiled.

## Why this matters more than it looks

The generator used to build its manifest by IMPORTING the compiled
bindings and reflecting on them. That works, and it puts the whole
build in one order: compile the C++ first, learn what it says second.
Every surface above - async, protocols, RPC, stubs - waited on a C++
compiler.

Reading the declaration inverts that. The manifest is known BEFORE
anything compiles, because the declaration already said everything
the manifest holds. Then the binding and the manifest are two
readings of one document rather than two stages of a pipeline.

## The vocabulary

`declare.py` is not a declaration: it is a library of decorators and
dataclasses with no side effects, which this module imports the way a
type checker would.

And it earns its import. A decorator's job is to write a field on a
`Decl`, so rather than restate that mapping here - `header` sets
`.header`, `produced(by=)` sets `.built_by` - this module APPLIES the
real decorator to a throwaway object and reads the result. The
mapping lives in one place, which is declare.py, and a decorator that
gains an argument needs no edit here.

## What it refuses

A name it cannot resolve, an annotation with no C++ spelling, a
decorator that is not from the vocabulary. Each stops with the line
number and the text that caused it. Guessing here would produce a
binding that compiles and is wrong.
"""

import ast
import contextlib
import difflib
import functools
import pathlib
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, get_args, get_origin

from huggorm_dsl import declare
from huggorm_dsl.declare import Cxx, Decl, Field

# Decorators that are Python's, not ours. A declaration may use them
# and they are read rather than applied.
BUILTIN_DECORATORS = frozenset({"property", "staticmethod", "classmethod",
                                "overload"})


# Where a declaration takes its vocabulary from. Named once: a
# declaration is read rather than imported, so this string is the only
# thing tying the two files together.
VOCABULARY = "huggorm_dsl.declare"

# `typing.Annotated`, which is where a fact about a TYPE goes. Named
# because the reader matches on the spelling in the source: the alias
# is never evaluated here, so `Annotated` is a bare name in a tree.
ANNOTATED = "Annotated"

# Where the declarations live. A declaration that names a type
# another declaration owns imports it from here, and the reader
# follows that import rather than being told the file.
DECLARATIONS = "huggorm_decl.decl"

# What a hand-written wire reconstructor is called. One name, because
# the wire layer asks for it by that name and the emitter binds it by
# that name.
FROM_PARTS = "_from_parts"


# Which declaration is being read, innermost last.
#
# A STACK rather than one path, because `_uses` calls `read` for every
# declaration this one imports - `decl/store.py` pulls in five - so an
# error is routinely raised while reading a file that is not the one
# the caller asked for. The old message said "line 183" and left a
# reader to work out which of nine files that was (tasks/061).
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
def reading(path: str) -> Iterator[None]:
    """Name the declaration a diagnostic raised in here belongs to.

    `read` does this for itself. This is for the emitters, which read
    a tree the corpus already parsed and can still refuse it -
    `pyerrors.entries` does, when the exception declaration will not
    import - and which otherwise raise a diagnostic with no file on
    it."""
    _READING.append(path)
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
    this codebase spends its effort removing."""

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

    @property
    def wire(self) -> str:
        """The `_wire_fields` spelling of this type.

        Derived, not declared. `T | None` is `T?`, because that is how
        the wire says presence; everything else crosses as itself."""
        inner, optional = self.python, False
        if inner.endswith("| None"):
            inner, optional = inner[:-len("| None")].strip(), True
        return f"{inner}?" if optional else inner


@dataclass(frozen=True)
class Param:
    """One declared parameter: its name, its type, and its default.

    `default` is the Python source of the default expression, or None
    when there is none. A binding that drops a default silently
    changes the Python signature, so it is read rather than ignored."""

    name: str
    type: Type
    default: str | None = None

    def __iter__(self):
        """Unpacks as `(name, type)`.

        Every emitter reads a parameter as that pair, and a default is
        a fourth thing only two of them care about. Rather than churn
        each call site into `pr.name, pr.type`, the pair stays the
        parameter's shape and the default is an attribute beside it."""
        return iter((self.name, self.type))


@dataclass(frozen=True)
class Method:
    """One declared method, with the C++ facts resolved.

    `params` and `ret` hold `Cxx`, not names: resolution happens once,
    here, so no emitter downstream has to know that `StrView` means
    `string_view` and owes the boundary a copy."""

    name: str
    # RAW, as written. Cleaning is a reader's convenience and an
    # emitter's problem: the manifest wants the literal text a class
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
    prop: bool = False
    # The C++ function a FREE function binds, from @binds.
    binds: str = ""
    # One of several registrations under one Python name, from
    # typing.@overload. nanobind resolves them by argument type at
    # call time, so the declaration lists each arity it accepts.
    overload: bool = False
    # The C++ data member behind this name, from @reads. Empty when
    # the accessor is a call rather than a field.
    reads: str = ""
    # Verbatim C++ for an accessor nothing can derive, from @cxx_body.
    cxx_body: str = ""
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
    # Empty means it declared none, which is what keeps a runtime
    # helper out of every generated form.
    policy: str = ""


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

    @property
    def is_words(self) -> bool:
        """Whether this is a vocabulary rather than a binding.

        A StrEnum with no C++ object behind it. It crosses as the
        string a member already IS, so nothing about it compiles."""
        return self.decl.kind == "words"

    @property
    def is_union(self) -> bool:
        """Whether this is a SUM of other declared types.

        Written as a module-level alias - `DerivedPath = StorePath |
        DerivedPathBuilt` - so it declares no methods and binds no C++
        class of its own. It is a Class here anyway, because that is
        what makes every emitter able to NAME it through `known`
        without learning a second kind of thing."""
        return self.decl.kind == "union"

    @property
    def is_value(self) -> bool:
        """Whether this class holds Python slots and no C++ at all.

        Two facts, not one, and an earlier version read `@produced`
        as if it were both. `@produced(by=...)` says only that nothing
        constructs one. `@binding(cxx=...)` says there is a C++ object
        behind it. PathInfo has the first and not the second, so it is
        a value; nix::Store has both, so it is a handle a factory
        opens - and emitting it as a value produced a module with the
        class in it twice."""
        return bool(self.decl.built_by) and not self.decl.cxx

    @property
    def is_produced(self) -> bool:
        """Whether nothing a caller writes can build one.

        Two halves, and `is_value` stood in for both until a produced
        value bound a real Nix type. Something else makes one -
        `@produced(by=...)` - AND this declaration offers no way in.

        `nix::Store` has the first half and not the second: it
        declares an `__init__`, and the emitter binds `open_store`
        behind it, so `Store(uri)` works and no stub may say NoReturn.

        `ctor is None` is the same test the emitter makes when it
        decides whether to write a constructor at all, so the surface
        and this cannot disagree."""
        return bool(self.decl.built_by) and self.ctor is None

    @property
    def constructs(self) -> bool:
        """Whether Python has a way to make one - "is there a door".

        The DERIVED question, computed once here because three layers
        used to ask it and all three asked `abstract` instead
        (tasks/061). `@abstract` states a fact about C++: the type has
        pure virtuals. Whether a caller can write `Store(uri)` is a
        different question, and conflating them meant the declaration
        could not state the true fact about nix::Store without
        deleting its constructor.

        Three ways to have no door, and each is a different sentence:

        - no `__init__` at all, so nothing was declared to call;
        - `@abstract` with no factory, so there is nothing to make;
        - `@produced(by=...)` and no `__init__`, which is the pair
          `is_produced` names - covered by the first test here.

        A FACTORY answers abstractness. `nix::Store` is abstract and
        `nix::openStore` hands back a concrete `LocalStore` or
        `UDSRemoteStore`, so the door is open and the C++ fact is
        still true."""
        if self.ctor is None:
            return False
        return bool(self.decl.built_by) or not self.decl.abstract

    @property
    def parts(self) -> list[tuple[Field, Method | None]]:
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
        two ways at this boundary. The WIRE spelling is what the manifest
        carries; the C++ spelling is what `_from_parts` takes, and only
        the accessor's own annotation has it."""
        by_name = {m.name: m for m in self.methods}
        if self.decl.fields:
            out = []
            for f in self.decl.fields:
                if isinstance(f, str):
                    m = by_name.get(f)
                    if m is None or m.ret is None:
                        raise TypeError(
                            f"{self.name}: '{f}' is declared a wire field and "
                            f"names no accessor of this class that answers "
                            f"anything.")
                    f = Field(f, m.ret.wire, read=f)
                out.append((f, by_name.get(f.read)))
            return out
        if self.decl.wire != "value":
            return []
        return [(Field(m.name, m.ret.wire, read=m.name), m)
                for m in self.methods if m.ret is not None and not m.local]


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
    # The EXCEPTION classes this declaration declares. Apart from
    # `classes` because nothing binds one either: an exception class
    # is plain Python that the errors emitter copies through, and it
    # carries no C++ object, no header and no methods a binding calls.
    #
    # Read at all because a VALUE may now hold one. A BuildResult's
    # failure arm is a `nix::BuildError`, so the accessor answers a
    # live Python exception - and an emitter that could not see the
    # name had nothing to resolve the annotation against.
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
        instead of one."""
        return tuple(fn for fn in self.functions
                     if not (fn.startup or fn.translator))

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


def _from_vocabulary(name: str, vocab: dict[str, str], node: ast.AST) -> Any:
    if name not in vocab:
        raise DeclarationError(
            node, f"'{name}' is not from declare. A declaration may only use "
                  f"the vocabulary it imported.")
    obj = getattr(declare, vocab[name], None)
    if obj is None:
        raise DeclarationError(
            node, f"declare has no '{vocab[name]}'. The import is stale.")
    return obj


def type_of(node: ast.expr, vocab: dict[str, str]) -> Type:
    """One annotation, resolved.

    Three answers, and the reader gives whichever the annotation
    supports rather than deciding per class. An earlier version had a
    per-class flag for this and it was wrong twice: first it keyed off
    `@produced`, which made nix::Store's C++ parameters Python; then
    off `wire`, which did the same to nix::StorePath. The fact was
    never a property of the class.

    A name in the vocabulary carries a C++ spelling. A capitalised
    name that is not vocabulary refers to another declared class.
    Anything else is a plain Python type, which is the whole truth
    about a field the C++ side already flattened.

    What this does NOT do is guess. A bare `str` where C++ is needed
    reaches an emitter with `cxx=None`, and the emitter refuses it
    there - which is the right place, because only the emitter knows
    whether it needed one."""
    # A string annotation is the forward reference a declaration needs
    # to name a type declared in another file. Unwrapped here so the
    # rest of the reader sees one spelling.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        spelled = node.value
    else:
        spelled = ast.unparse(node)
    if isinstance(node, ast.Name) and node.id in vocab:
        alias = _from_vocabulary(node.id, vocab, node)
        if get_origin(alias) is not None:
            for meta in get_args(alias)[1:]:
                if isinstance(meta, Cxx):
                    # The PYTHON spelling of an alias is its first
                    # arg: `Annotated[str, Cxx("string_view")]` is a
                    # str. QUALIFIED when it is not a builtin, because
                    # `Path` alone is ambiguous and `pathlib.Path` is
                    # what an annotation has to say to typecheck.
                    inner = get_args(alias)[0]
                    name = inner.__name__
                    if inner.__module__ != "builtins":
                        name = f"{inner.__module__}.{name}"
                    return Type(python=name, cxx=meta)
        raise DeclarationError(
            node, f"'{node.id}' is vocabulary but carries no C++ spelling. "
                  f"Annotate the alias with Cxx(...) in declare.py.")
    bare = (spelled.replace(" | None", "")
            .removeprefix("list[").removeprefix("dict[str, ").rstrip("]"))
    if bare in vocab:
        # A QUOTED annotation means what the same annotation means
        # unquoted. `-> I64` and `-> "I64 | None"` name one width, and
        # a declaration has to quote the second: `I64 | None` is a
        # union of an Annotated alias, which Python builds eagerly and
        # a reader of the source cannot see the C++ through.
        #
        # Without this the alias fell through to the branch below,
        # which reads a capital letter as another declared class - so
        # `"I64 | None"` asked the emitter for a class called I64.
        held = type_of(ast.Name(id=bare), vocab)
        return Type(python=spelled.replace(bare, held.python),
                    cxx=held.cxx)
    if hasattr(declare, bare):
        # Vocabulary this file did NOT import. It resolves for a
        # reader of the source, because Python finds it in declare.py,
        # and it resolves for nothing here - `vocab` is only what this
        # file imported, which is what keeps a declaration from
        # gaining phantom vocabulary by convenience.
        #
        # Refused HERE rather than in the emitter, which sees only a
        # capitalised name and says "names a class this run has not
        # read" - true, unhelpful, and pointing at the wrong fix.
        raise DeclarationError(
            node, f"'{bare}' is vocabulary, and this file does not import "
                  f"it. Add it to the `from {VOCABULARY} import` above, or "
                  f"name a class some declaration declares.")
    # The reader records a reference to another declared class;
    # resolving it needs that declaration, which only the emitter has.
    if bare[:1].isupper():
        return Type(python=spelled, bound=True)
    return Type(python=spelled)


# -- literals -------------------------------------------------------------

def _value(node: ast.expr, vocab: dict[str, str]) -> Any:
    """A decorator argument, as the object it denotes.

    Literals go through ast.literal_eval, which evaluates no code. A
    call is allowed only when it names something in the vocabulary -
    `Field("base_name", "str", read="to_string")` - and then the real
    Field is built, so its own defaults and validation apply."""
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise DeclarationError(node, f"cannot read {ast.unparse(node)}")
        target = _from_vocabulary(node.func.id, vocab, node)
        args = [_value(a, vocab) for a in node.args]
        kwargs = {k.arg: _value(k.value, vocab)
                  for k in node.keywords if k.arg is not None}
        return target(*args, **kwargs)
    if isinstance(node, ast.Tuple):
        return tuple(_value(e, vocab) for e in node.elts)
    if isinstance(node, ast.Dict):
        # Recursed rather than literal_eval'd, because a value may be
        # a vocabulary call: `wraps={"StorePath": Wrap(...)}` is a
        # dict whose values are not constants.
        return {_value(k, vocab): _value(v, vocab)
                for k, v in zip(node.keys, node.values, strict=True)
                if k is not None}
    try:
        return ast.literal_eval(node)
    except ValueError as exc:
        raise DeclarationError(
            node, f"'{ast.unparse(node)}' is not a constant. A declaration "
                  f"holds facts, not expressions.") from exc


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
            continue
        if not name or name in BUILTIN_DECORATORS:
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


def _apply(decorators: list[ast.expr], vocab: dict[str, str],
           target: Any, kind: str) -> Any:
    """Run the file's decorators against a stand-in.

    The declaration's own class is never built. What gets decorated is
    a throwaway that carries nothing, so the only thing that happens
    is declare.py writing fields onto it - which is the mapping this
    reader would otherwise have to restate and keep in step.

    Bottom-up, like Python: `@header` above `@binding` means binding
    applies first, and a decorator that overwrote a field would win in
    the same order a reader expects.

    `kind` is what is being decorated, so the table can say where a
    marker is legal. Every marker on every target goes through here,
    which is why the check belongs here and not at each call site."""
    _check_markers(decorators, kind)
    for node in reversed(decorators):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise DeclarationError(node, f"cannot read @{ast.unparse(node)}")
            fn = _from_vocabulary(node.func.id, vocab, node)
            args = [_value(a, vocab) for a in node.args]
            kwargs = {k.arg: _value(k.value, vocab)
                      for k in node.keywords if k.arg is not None}
            # The marker's OWN signature, enforced by calling it. The
            # table says where a marker is legal and how often; how
            # many arguments it takes is written once, in `declare.py`,
            # as the decorator's parameter list - so restating it as
            # data would be the same fact twice.
            #
            # Wrapped, because Python's own message has no position on
            # it: `@reads("a", "b")` said "reads() takes 1 positional
            # argument but 2 were given" and named neither the file nor
            # the line.
            try:
                target = fn(*args, **kwargs)(target)
            except TypeError as exc:
                raise DeclarationError(
                    node, f"@{node.func.id}: {exc}") from exc
        elif isinstance(node, ast.Name):
            if node.id in BUILTIN_DECORATORS:
                # Python's own words, and they mean here what they
                # mean everywhere. @property says an accessor is an
                # attribute rather than a call.
                continue
            target = _from_vocabulary(node.id, vocab, node)(target)
        else:
            raise DeclarationError(node, f"cannot read @{ast.unparse(node)}")
    return target


# -- methods --------------------------------------------------------------

def _body(node: ast.FunctionDef) -> str:
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

    An empty body is what says the emitter DERIVES the whole binding,
    which is fourteen of the thirty-eight. Presence of `Cxx` is the
    whole distinction, and unlike a decorator it cannot be
    half-stated - there is no way to write the marker and forget the
    body, or the body and forget the marker."""
    seen = ""
    for i, stmt in enumerate(node.body):
        if isinstance(stmt, ast.Expr):
            value = stmt.value
            if i == 0 and isinstance(value, ast.Constant) and isinstance(
                    value.value, str):
                continue                       # the docstring
            if (isinstance(value, ast.Call)
                    and isinstance(value.func, ast.Name)
                    and value.func.id == "Cxx"):
                if seen:
                    raise DeclarationError(
                        stmt, f"{node.name}: one Cxx(...) per body. Two "
                              f"bodies is two bindings.")
                if len(value.args) != 1 or not isinstance(
                        value.args[0], ast.Constant):
                    raise DeclarationError(
                        stmt, f"{node.name}: Cxx() takes one string "
                              f"literal. The C++ is carried, not built.")
                seen = str(value.args[0].value)
                continue
            if isinstance(value, ast.Constant) and value.value is Ellipsis:
                continue                       # `...`, an empty body
        raise DeclarationError(
            stmt, f"{node.name}: a declaration body is a docstring, then "
                  f"at most one Cxx(...). {ast.unparse(stmt)!r} is "
                  f"neither, and nothing would emit it.")
    return seen


def _method(node: ast.FunctionDef, vocab: dict[str, str],
            bound: bool = True, bound_kind: bool = True) -> Method:
    """One declared function.

    `bound=False` for a function with no `self` to skip. That is not
    the same question as WHERE it lives: `_from_parts` is a class
    member with no self, so `bound_kind` carries the marker-table
    kind separately.
    Reading a free function as a method silently drops its first
    parameter, which is how `open_store(uri)` first emitted without
    the `"uri"_a` that makes the parameter usable by keyword."""
    args = node.args
    if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
        raise DeclarationError(
            node, f"{node.name}: a bound method takes plain positional "
                  f"parameters. C++ has no *args.")
    # Defaults bind to the LAST parameters, so line them up from the
    # right - `f(a, b=1)` has one default and it belongs to b.
    positional = args.args[1:] if bound else args.args
    pad = len(positional) - len(args.defaults)
    params = []
    for i, arg in enumerate(positional):
        if arg.annotation is None:
            raise DeclarationError(
                arg, f"{node.name}({arg.arg}): every parameter states its "
                     f"type.")
        d = args.defaults[i - pad] if i >= pad else None
        params.append(Param(arg.arg, type_of(arg.annotation, vocab),
                            ast.unparse(d) if d is not None else None))

    ret: Type | None = None
    returns = node.returns
    if returns is not None and not (isinstance(returns, ast.Constant)
                                    and returns.value is None):
        ret = type_of(returns, vocab)

    # A method decorator writes an attribute on a function, so the
    # same trick works: decorate a stand-in and read what was written.
    def probe() -> None: ...
    marked = _apply(node.decorator_list, vocab, probe,
                    "method" if bound_kind else "free")
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
        prop=any(isinstance(d, ast.Name) and d.id == "property"
                 for d in node.decorator_list),
        binds=getattr(marked, "_binds", ""),
        overload=any(isinstance(d, ast.Name) and d.id == "overload"
                     for d in node.decorator_list),
        reads=getattr(marked, "_reads", ""),
        cxx_body=_body(node),
        local=bool(getattr(marked, "_local", False)),
        headers=tuple(getattr(marked, "_needs", ())),
        spells=tuple(getattr(marked, "_spells", ())),
        startup=bool(getattr(marked, "_startup", False)),
        translator=bool(getattr(marked, "_translator", False)),
        policy=getattr(marked, "_policy", ""),
    )


def _members(node: ast.ClassDef) -> tuple[Member, ...]:
    """The words of a vocabulary, in the order it lists them.

    `NAME = "value"`, and the docstring that may follow it. Python
    puts an attribute's documentation under the assignment rather
    than inside it, so the two are read as one pair here."""
    out: list[Member] = []
    for i, item in enumerate(node.body):
        if not isinstance(item, ast.Assign):
            continue
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


def targets_name(item: ast.Assign) -> str:
    target = item.targets[0]
    assert isinstance(target, ast.Name)
    return target.id


def _class(node: ast.ClassDef, vocab: dict[str, str],
           where: str = "", live: set[int] | None = None) -> Class:
    holder = _apply(node.decorator_list, vocab, type(node.name, (), {}),
                    "class")
    decl: Decl = holder.__dict__.get("_decl", Decl())
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
    if node.bases:
        if len(node.bases) > 1:
            raise DeclarationError(
                node, f"{node.name}: one base. Every hierarchy this binds "
                      f"is single inheritance, and C++ multiple "
                      f"inheritance through a Python type is a different "
                      f"problem from the one a declaration is for.")
        base = node.bases[0]
        if not isinstance(base, ast.Name):
            raise DeclarationError(
                node, f"{node.name}: a base is a NAME another declaration "
                      f"declares, not {ast.unparse(base)!r}.")
        decl.base = base.id
    # `@needs` writes onto whatever it decorates, and on a class that
    # is the stand-in rather than the Decl - so it is read here
    # instead of being restated in declare.py, which is the same trick
    # every other decorator gets.
    decl.headers = tuple(holder.__dict__.get("_needs", ()))

    if decl.kind == "words":
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
    # ...with any `NIX_VERSION` branch already chosen by the import.
    for item in _resolve(node.body, live):
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name == FROM_PARTS:
            # Not a method. It takes the parts the wire carried, and
            # the emitter writes that signature from the field list -
            # so the declaration writes the BODY and nothing else. A
            # `self` here would be the object it exists to build.
            from_parts = _method(item, vocab, bound=False)
            if from_parts.params:
                raise DeclarationError(
                    item,
                    f"{node.name}.{FROM_PARTS} takes no parameters here. "
                    f"The wire fields ARE its parameters, and the emitter "
                    f"writes them from the field list so the two cannot "
                    f"disagree. The body reads them by name.")
            continue
        if item.name == "__init__":
            ctor = _method(item, vocab)
            if decl.built_by and ctor.params:
                # A `@produced(by=X)` class is built by X, so X owns
                # the signature. Declaring it twice is how the
                # `uri="auto"` default died: `open_store` carried it,
                # `__init__` did not, and the emitter read the wrong
                # one - so `Store()` raised, `AsyncStore()` required
                # an argument, and the stub and the manifest agreed
                # with each other about the wrong thing.
                #
                # The `__init__` is still worth writing: it is where
                # the PROSE goes, and a caller reading the declaration
                # looks for it under the name they will call. Only the
                # parameters are refused.
                raise DeclarationError(
                    item,
                    f"{node.name}.__init__ declares parameters, but "
                    f"@produced(by={decl.built_by!r}) says "
                    f"{decl.built_by} builds one - so {decl.built_by} "
                    f"owns the signature. Move them there and leave "
                    f"the docstring here.")
        elif not item.name.startswith("__"):
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
                methods.append(_method(item, vocab))
            except DeclarationError as e:
                _survive(e)
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
    round-trip test can, and it does (tasks/056). It catches the
    forgetting, which is the failure that costs nothing to catch here
    and a debugging session to catch there.

    A false positive costs one comment naming the field."""
    assert cls.from_parts is not None
    seen = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*",
                          cls.from_parts.cxx_body))
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


@functools.cache
def load(path: str) -> ModuleType | None:
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

    `None` when the file will not import. That is not a failure: a
    declaration is a document first, and one that cannot be imported
    still parses - so a caller falls back to the tree alone.

    CACHED by path, because two readers now want the same module and
    executing a declaration twice would run its decorators twice."""
    import importlib.util

    name = f"_huggorm_decl_{pathlib.Path(path).stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None
    return mod


def _live(path: str) -> set[int] | None:
    """The first line of every class and function the IMPORT kept.

    A declaration may branch on `NIX_VERSION`, and Python resolves
    that during the import. This asks the resulting module which
    definitions survived, and the answer is a set of LINES.

    Lines, not names: both arms of an `if` define the same name, so a
    name match keeps both and the emitter binds the method twice.
    `co_firstlineno` is the first DECORATOR's line when a definition
    has decorators and the `def`/`class` line when it has none, so a
    caller matches either.

    `None` when the file will not import. That is not a failure here:
    a declaration is a document first, and one that cannot be
    imported still parses - so the reader falls back to the tree
    alone and every `if` arm is read. `_resolve` says what that
    costs."""
    mod = load(path)
    if mod is None:
        return None
    name = mod.__name__

    # Only what THIS file defines. A declaration imports its
    # vocabulary - `binding`, `header`, `cxx_body` - and those are
    # functions too, whose `co_firstlineno` points into declare.py.
    # Counting them made every one of their lines an orphan, which is
    # what the reconcile gate said the first time it ran.
    here = str(pathlib.Path(path).resolve())
    lines: set[int] = set()

    def note(obj: object) -> None:
        # A DESCRIPTOR holds the function rather than being one, so
        # the function is asked for it. `property`, `staticmethod` and
        # `classmethod` are the three a declaration can write, and
        # none of them carries `__code__`.
        #
        # Without this a `@property` accessor named no line, `_resolve`
        # dropped its node as if the import had dropped it, and the
        # accessor vanished from the binding, from `_parts`, from
        # `__repr__` and from `_wire_fields`. Measured on
        # `PathInfo.registration_time`: the only complaint was
        # `'registration_time' was not declared in this scope`, from
        # the hand-written `_from_parts` body that still named it - so
        # a class whose `_from_parts` is derived would have lost the
        # field in silence (tasks/075).
        obj = getattr(obj, "fget", None) or getattr(obj, "__func__", obj)
        code = getattr(obj, "__code__", None)
        if code is not None and code.co_filename == here:
            lines.add(code.co_firstlineno)

    for obj in vars(mod).values():
        note(obj)
        if isinstance(obj, type) and obj.__module__ == name:
            lines.add(getattr(obj, "__firstlineno__", 0))
            for member in vars(obj).values():
                note(member)
    lines.discard(0)
    return lines


def _reconcile(tree: ast.Module, live: set[int] | None, path: str) -> None:
    """Every definition the import kept must exist in the tree.

    The two readings come from one file in one call, so they cannot
    drift the way two declarations of one fact drift - that was F1 and
    F7a. What they CAN do is stop lining up, and the way that happens
    is a Python release changing what `co_firstlineno` points at.

    Then nothing matches, `_resolve` keeps nothing, and the module
    emits an empty binding. Silently. This is the check that makes it
    loud.

    It reads one direction only, and the reason it gives for that -
    "the other direction cannot happen" - was wrong. A tree node the
    import KEPT can still name no live line, because `_live` reads
    `__code__` and a descriptor has none. That is `nodes - live`, and
    nothing here would have seen it: a `@property` accessor was
    dropped in silence until `_live` learnt to look through the
    descriptor (tasks/075). The direction is still not worth a gate -
    a dead `if` arm is exactly `nodes - live` and is not an error -
    so the fix belongs in `_live` rather than here."""
    if not live:
        return
    nodes = {n.lineno for n in ast.walk(tree)
             if isinstance(n, ast.ClassDef | ast.FunctionDef)}
    nodes |= {d.lineno for n in ast.walk(tree)
              if isinstance(n, ast.ClassDef | ast.FunctionDef)
              for d in n.decorator_list}
    orphans = sorted(live - nodes)
    if orphans:
        raise DeclarationError(
            tree,
            f"{pathlib.Path(path).name}: the import kept definitions at "
            f"lines {orphans} and the tree has no node there. The two "
            f"readings have stopped lining up - most likely "
            f"`co_firstlineno` no longer points where this assumes.")


def _resolve(body: list[ast.stmt], live: set[int] | None) -> list[ast.stmt]:
    """One body with its `if` arms already chosen.

    Nothing here evaluates a condition. Python did that during the
    import, and this keeps what Python kept - matched by the line a
    definition starts on.

    With `live` unknown the `if` is flattened whole, which reads both
    arms and will usually declare a name twice. That is loud rather
    than silent: the emitter binds it twice and the build says so."""
    out: list[ast.stmt] = []

    def walk(nodes: list[ast.stmt]) -> None:
        for n in nodes:
            if isinstance(n, ast.If):
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, ast.ClassDef | ast.FunctionDef):
                own = {n.lineno} | {d.lineno for d in n.decorator_list}
                if live is None or own & live:
                    out.append(n)
            else:
                out.append(n)

    walk(body)
    return out


def read(path: str) -> Module:
    """One declaration file, read twice.

    `ast.parse` gives the tree, which is what every emitter reads and
    what `pyi.py` transforms. The IMPORT gives one fact the tree
    cannot: which definitions survive a `NIX_VERSION` branch.

    The import is the authority on WHAT exists. The tree is the source
    for HOW to render it. No fact is taken from both."""
    _READING.append(path)
    try:
        return _read(path)
    finally:
        _READING.pop()


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
    (tasks/073)."""
    _READING.append(path)
    try:
        return _chosen(path)[0]
    finally:
        _READING.pop()


def _chosen(path: str) -> tuple[ast.Module, set[int] | None]:
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
    """`read` with the path already on the stack."""
    tree, live = _chosen(path)
    body = tree.body
    vocab = _vocabulary(tree)
    stem = pathlib.Path(path).stem
    # SIBLINGS, so one bad class does not hide the next. A class is
    # read in full or not at all - the failure is recorded, its name
    # is marked unsound, and the loop goes on.
    classes = []
    for n in body:
        if not (isinstance(n, ast.ClassDef) and n.decorator_list):
            continue
        try:
            classes.append(_class(n, vocab, stem, live))
        except DeclarationError as e:
            _survive(e, unsound=n.name)
    # Module-level functions are FREE bindings - nanopynix has 72 of
    # them, `m.def("open_store", &open_store_uri, "uri"_a)` and its
    # kind. Only decorated ones: an undecorated def at module level is
    # a helper the declaration wrote for itself.
    functions = []
    for n in body:
        if not (isinstance(n, ast.FunctionDef) and n.decorator_list):
            continue
        try:
            functions.append(
                _method(n, vocab, bound=False, bound_kind=False))
        except DeclarationError as e:
            _survive(e)
    unions = _unions(body, stem, vocab)
    uses = _uses(tree, pathlib.Path(path).parent)
    # ...checked once the arms can be resolved, which needs the
    # imports this file made and the classes it declares itself.
    #
    # THE ONE CROSS-CLASS CHECK IN THIS READER, which is why bogus
    # errors have one source and one cure. Every other refusal above
    # is about a single class or this file's own vocabulary.
    resolvable = {**uses, **{c.name: c for c in classes},
                  **{u.name: u for u in unions}}
    # The assignment each union was written as, so a refusal points at
    # the line rather than at the file. `_unions` builds a `Class`,
    # which carries no position - and the alternative was a field on
    # the dataclass for the benefit of one message.
    at = {t.id: item for item in body if isinstance(item, ast.Assign)
          for t in item.targets if isinstance(t, ast.Name)}
    for u in unions:
        try:
            _check_arms(u, resolvable, at.get(u.name, tree))
        except DeclarationError as e:
            _survive(e, unsound=u.name)
    return Module(
        name=stem,
        doc=ast.get_docstring(tree, clean=False) or "",
        classes=tuple(classes),
        errors=_errors(body, stem),
        functions=tuple(functions),
        vocabulary=vocab,
        unions=unions,
        uses=uses,
    )


def _arms(node: ast.expr) -> tuple[str, ...] | None:
    """`A | B` as ("A", "B"), or None when this is not a union at all.

    Only a chain of `|` over NAMES. `str | None` is presence and is
    read by `type_of`, not here; a lowercase name is a scalar, which
    has no distinguishable arms and is refused where the arms are
    checked."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        left, right = _arms(node.left), _arms(node.right)
        if left is None or right is None:
            return None
        return left + right
    if isinstance(node, ast.Name):
        return (node.id,)
    return None


def _annotated(node: ast.expr) -> tuple[ast.expr, list[ast.expr]]:
    """An `Annotated[X, ...]` as X and its metadata, or the node bare.

    The alias STAYS a type alias. `Annotated[A | B, Variant(...)]` is
    `A | B` to a type checker and to anyone reading the file, so the
    union keeps the one property that made it an alias rather than a
    class - and the C++ facts ride where this DSL already puts facts
    about a type."""
    if not (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == ANNOTATED):
        return node, []
    if not isinstance(node.slice, ast.Tuple) or not node.slice.elts:
        return node, []
    return node.slice.elts[0], list(node.slice.elts[1:])


def _unions(body: list[ast.stmt], where: str,
            vocab: dict[str, str]) -> tuple[Class, ...]:
    """Every module-level union alias, as a Class the emitters can name.

    A union is written as the alias it is:

        DerivedPath = StorePath | DerivedPathBuilt

    Real Python, so a reader of the file and a type checker both see a
    union; and the alias NAME is what the wire calls the message, so
    nothing has to invent one.

    It comes back as a `Class` because that is what lets every emitter
    resolve it through `known` - the same way a vocabulary does -
    rather than learning a second kind of thing."""
    out: list[Class] = []
    for i, item in enumerate(body):
        if not isinstance(item, ast.Assign):
            continue
        if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
            continue
        held, meta = _annotated(item.value)
        arms = _arms(held)
        if arms is None or len(arms) < 2:
            continue
        name = item.targets[0].id
        variant = _variant(name, meta, arms, vocab, item)
        doc = ""
        nxt = body[i + 1] if i + 1 < len(body) else None
        if (isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)):
            doc = nxt.value.value
        out.append(Class(
            name=name, doc=doc, ctor=None, module=where,
            decl=Decl(name=name, kind="union", arms=arms, wire="value",
                      variant=variant),
        ))
    return tuple(out)


def _variant(name: str, meta: list[ast.expr], arms: tuple[str, ...],
             vocab: dict[str, str], node: ast.AST) -> declare.Variant | None:
    """The `Variant(...)` on a union's alias, checked against its arms.

    None when the alias carries none, which stays legal: a union
    whose C++ variant holds its arms as themselves needs no fact
    stated, and the emitter refuses only where it actually needs one.

    A `wraps` key that names no arm is refused here. It is the
    mistake a rename makes - the arm moves, the wrap does not - and
    it would otherwise emit a conversion for a type the variant does
    not hold."""
    found = [_value(m, vocab) for m in meta]
    variants = [v for v in found if isinstance(v, declare.Variant)]
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
    for arm in cls.decl.arms:
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
                      f"union names types some declaration declares - a "
                      f"scalar has no distinguishable arms, so `str | int` "
                      f"is not a union but a mistake.")
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
        if other.decl.wire != "value":
            raise DeclarationError(
                node, f"{cls.name}: '{arm}' crosses as a {other.decl.wire or 'proxy'}, "
                      f"not as a value. An arm that granted a lease would "
                      f"make every union a bulk-lease problem (tasks/031).")


def _errors(body: list[ast.stmt], stem: str) -> tuple[Class, ...]:
    """The EXCEPTION classes this file declares, in declared order.

    An error declaration wears no decorator - `cxx = "nix::Error"` is
    a bare assignment, because there is no behaviour to mark - so the
    class loop above skips every one of them. That was fine while
    nothing but the errors emitter read the file, and it stopped
    being fine when a VALUE gained a field of one.

    Derived from the BASE, which is the only thing that says what an
    exception is: a class deriving from `Exception`, or from one this
    file already recognised. Nothing is listed and no decorator is
    invented; the hierarchy the file already writes IS the answer.

    A NAME and nothing else. These carry no C++ object, no header and
    no methods a binding calls, so an emitter wants them to resolve an
    annotation and for nothing else - the errors emitter reads the
    tree itself and is untouched by this.
    """
    out: list[Class] = []
    known = {"Exception"}
    for node in body:
        if not isinstance(node, ast.ClassDef) or node.decorator_list:
            continue
        bases = {b.id for b in node.bases if isinstance(b, ast.Name)}
        if not bases & known:
            continue
        known.add(node.name)
        decl = Decl()
        decl.name = node.name
        decl.kind = "error"
        out.append(Class(
            name=node.name,
            doc=ast.get_docstring(node, clean=False) or "",
            decl=decl,
            ctor=None,
            module=stem,
        ))
    return tuple(out)


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
