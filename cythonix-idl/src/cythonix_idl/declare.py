"""
The vocabulary a binding declaration is written in.

A declaration file is plain Python: a class with typed methods, `...`
bodies and docstrings. Everything C++ needs that Python cannot say
arrives one of two ways, and the split is the point.

**Annotated type aliases** carry facts about a TYPE. `StrView` is
`std::string_view` wherever it appears, so the fact is written once
and read by name. A per-method `Annotated[str, Cxx("string_view")]`
would say the same thing and drown the signature it describes.

**Decorators** carry facts about a METHOD or a CLASS: which header it
comes from, what C++ calls it, whether it can block. A plain `def`
takes a decorator, which is exactly what a cdef method cannot - and
the reason the current bindings put class markers in the class body
and free-function markers in decorators.

What is NOT here is any statement about how to WRITE the binding. The
declaration says `to_string` returns a `string_view`; that a view must
be copied before it reaches Python is the emitter's rule, because it
is a fact about the boundary rather than about nix::StorePath.
"""

import pathlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


@dataclass(frozen=True)
class Cxx:
    """How a Python type is spelled in C++.

    `copy` says what the boundary owes it. "view" means the value
    points into storage this binding does not own, so it is copied
    before it reaches Python - a view outliving its owner is a
    dangling pointer, not an exception."""

    spelling: str
    copy: str = "value"


# The types a Nix binding actually names. Written once, read by name.
Str = Annotated[str, Cxx("string")]
StrView = Annotated[str, Cxx("string_view", copy="view")]
Bint = Annotated[bool, Cxx("bint")]
# Bytes, not text, and a std::string carries both. The difference is
# above the boundary: a store holds FILES, and the hash that names a
# store path is a hash of exactly these bytes - so a caller who has
# text has to say which encoding made it a file.
Bytes = Annotated[bytes, Cxx("string")]
# Widths. Python has one integer type and C++ has many, so a plain
# `int` says what a CALLER sees and nothing about what crosses. These
# say both: `int` above the boundary, a fixed width at it.
#
# Which width is not a preference. `nar_size` is upstream's uint64_t
# and `registration_time` is a time_t the shim narrows to int64_t, so
# a declaration that said `int` for both would leave the emitter to
# guess, and it would guess the same for two fields that differ.
U64 = Annotated[int, Cxx("uint64_t")]
I64 = Annotated[int, Cxx("int64_t")]
# A std::string at the boundary and a pathlib.Path above it. Not the
# same as `Str`, and the difference is the whole point of the two:
# `print_store_path` answers in the STORE's terms, which may name a
# directory this machine does not have, and `real_path` answers where
# the bytes are here. Only the second is a path a caller can open.
Path = Annotated[pathlib.Path, Cxx("string")]


@dataclass(frozen=True)
class Field:
    """One declared part of a wire value.

    `read` names the accessor that produces it, because the field name
    and the accessor need not agree: a StorePath's part is called
    `base_name` and is read by `to_string`."""

    name: str
    type: str
    read: str


@dataclass
class Decl:
    """Everything the emitter knows about one bound class."""

    name: str = ""
    header: str = ""
    cxx: str = ""
    built_by: str = ""
    threading: str = "pool"
    blocking: bool = True
    wire: str = ""
    fields: tuple[Field, ...] = ()
    compare: str = ""
    text: str = ""
    shown: str = ""
    order: bool = False
    custom: dict[str, str] = field(default_factory=dict)
    # What KIND of declaration this is. "class" binds a C++ type or
    # holds a produced value's slots; "words" is a vocabulary - a
    # StrEnum whose members ARE the strings a Nix parser takes, with
    # no C++ object behind it at all.
    kind: str = "class"
    # Where the words come from, for a vocabulary. Prose only: the
    # emitted module names it so a reader can check the list.
    parsed_by: str = ""
    # The class this one derives from, by DECLARED name. One base:
    # every hierarchy this binds is single inheritance, and C++
    # multiple inheritance through a Python type is a different
    # problem from the one a declaration is for.
    base: str = ""
    # Whether Python may construct one. An abstract base still gets a
    # class, an async wrapper and a wire identity - a caller holds a
    # MockStore most of the time - but calling it would build an
    # object with no implementation behind it.
    abstract: bool = False
    # How a value TREE is walked, for a type that holds others. Read
    # by the RPC layer, so no layer above the declaration knows what
    # the type is or which of its methods do what.
    tree: str = ""
    # What Python holds one of these THROUGH. Empty for the usual
    # case, where Python owns the object outright. "shared_ptr" for a
    # class whose factory hands back a reference-counted handle -
    # nix::openStore does, and a store has to stay open for as long
    # as the object naming it does.
    holder: str = ""


def derives(base: str) -> Callable[[type], type]:
    """The class this one derives from, by declared name.

    C++ inheritance, not Python's. The declaration names a base and
    the emitter passes it to `nb::class_`, which is what makes a
    method declared once on the base reachable from every leaf - and
    what lets a free function taking the base accept a leaf.

    A declaration states no Python base class of its own: `class
    MockLocalStore(MockStore)` in a declaration file would be a
    Python hierarchy among objects that are never constructed, and
    `read.py` refuses one so the two cannot drift."""
    def apply(cls: type) -> type:
        _decl(cls).base = base
        return cls
    return apply


def abstract(cls: type) -> type:
    """Python may not construct one of these.

    The base of a hierarchy whose leaves are the implementations. It
    still gets a class, because a caller holds the base far more
    often than a leaf - what it does not get is a constructor."""
    _decl(cls).abstract = True
    return cls


def tree(**shape: object) -> Callable[[type], type]:
    """How a value TREE is walked, for a type that holds others.

    A map the RPC layer reads so that no layer above this declaration
    knows what the type is or which of its methods do what: `kind`
    names the accessor that says what a node is, and its answer picks
    one of the branches beside it.

    Carried as the SOURCE of the literal, because it is data rather
    than a shape this vocabulary should learn to describe. The
    emitter builds the same structure in the binding."""
    def apply(cls: type) -> type:
        _decl(cls).tree = repr(shape)
        return cls
    return apply


def virtual[F: Callable[..., Any]](fn: F) -> F:
    """A C++ method a Python subclass may override.

    What makes a trampoline necessary and what says which methods it
    forwards. Without one, a Python override is invisible to C++: a
    free function taking the base calls the C++ implementation and
    never sees it."""
    fn._virtual = True  # type: ignore[attr-defined]
    return fn


def pure[F: Callable[..., Any]](fn: F) -> F:
    """A virtual with NO implementation behind it.

    `@virtual` says a Python subclass may override; this says there is
    nothing to fall back to when none does. C++ spells it `= 0`, and
    the difference is not cosmetic: a trampoline that forwards to the
    base implementation of a pure virtual is a link error, because
    there is no such implementation to link against.

    Implies `@virtual`. A pure virtual is one by definition."""
    fn._virtual = True  # type: ignore[attr-defined]
    fn._pure = True  # type: ignore[attr-defined]
    return fn


def startup[F: Callable[..., Any]](fn: F) -> F:
    """Call this once, when the module is imported.

    Not exported. It is what a library demands before anything else
    in it is touched - `initLibStore`, the collector's `init` - and a
    caller has no business calling it twice or forgetting to call it
    once.

    Declared rather than assumed, because which one a module needs is
    a fact about the library it binds. The emitter used to name
    libstore's in every extension, which is wrong for a module that
    does not bind libstore."""
    fn._startup = True  # type: ignore[attr-defined]
    return fn


def translator[F: Callable[..., Any]](fn: F) -> F:
    """The C++ that turns a library exception into a Python one.

    Registered once for the module, and not exported. The default is
    already right for a library that throws std::exception - nanobind
    maps that to RuntimeError - so a declaration says this only when
    the library has a hierarchy worth keeping."""
    fn._translator = True  # type: ignore[attr-defined]
    return fn


def _decl(cls: type) -> Decl:
    if "_decl" not in cls.__dict__:
        cls._decl = Decl()  # type: ignore[attr-defined]
    d: Decl = cls.__dict__["_decl"]
    return d


def header(path: str) -> Callable[[type], type]:
    """The C++ header this class is declared in."""
    def apply(cls: type) -> type:
        _decl(cls).header = path
        return cls
    return apply


def produced(by: str) -> Callable[[type], type]:
    """This class is built by something else, never constructed.

    A produced value holds no C++ object at all: the object that made
    it flattened one, and what is left is Python slots. So the shape
    is different from a bound class rather than a variation on it -
    no header, no pointer, an __init__ that raises, and a _from_parts
    that fills a __new__ instance because there is no constructor to
    call.

    `by` names what makes one, and it is not decoration: it is the
    sentence the refusing __init__ raises with, so a caller who
    guesses wrong is told where to look."""
    def apply(cls: type) -> type:
        _decl(cls).built_by = by
        return cls
    return apply


def words(parsed_by: str = "") -> Callable[[type], type]:
    """This class is a VOCABULARY: the words a Nix parser takes.

    A StrEnum, so a member IS the string libstore parses. Passing
    `ContentAddressMethod.FLAT` and passing `"flat"` are the same
    call, which is what keeps such a class a convenience rather than
    a layer - it names what libstore already accepts, so an editor
    can offer the words and a typo fails before the call.

    There is nothing to compile. The values are Nix's words, and the
    binding hands one straight to a parser rather than translating
    it, so the emitted module is plain Python: no pointer, no header,
    no shim.

    `parsed_by` names the C++ that takes them. Prose only - the
    emitted module says it so a reader can check the list against
    upstream - but it is the one fact that says where the words came
    from, which is not derivable from the members."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.kind, d.parsed_by = "words", parsed_by
        return cls
    return apply


def binding(cxx: str = "", threading: str = "pool", holder: str = "",
            blocking: bool = True) -> Callable[[type], type]:
    """The C++ class this binds, and how it may be called.

    `blocking=False` means no method here can wait: every one is a
    read of memory the object already owns. It decides whether the
    emitter writes `with nogil:` and whether anything above needs a
    thread to hop to.

    `holder` is what Python holds one THROUGH, and it is empty for
    almost everything. "shared_ptr" is for a class whose factory
    hands back a reference-counted handle: nix::openStore does, and a
    store has to stay open for as long as the object naming it
    does."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.cxx, d.threading, d.blocking = cxx, threading, blocking
        d.holder = holder
        return cls
    return apply


def wire_value(fields: tuple[Field, ...] = (), compare: str = "parts",
               text: str = "", order: bool = False,
               shown: str = "") -> Callable[[type], type]:
    """This class serializes, and here is what it is made of.

    `compare="cxx"` says the C++ class carries its own equality, so
    the binding declares the operator instead of comparing the parts
    in Python. `order` asks for the comparison operators, which only a
    type with a natural order should want.

    `text` and `shown` are different questions, and conflating them
    was a bug. `text` names the accessor `str()` answers with, and it
    is a CONVERSION: only a value that IS a string should have one, so
    a StorePath does and a nine-field record does not. `shown` names
    the accessor a repr identifies the value by, which every value
    has. A value with a `text` is shown by it unless it says
    otherwise."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.wire, d.fields, d.compare = "value", fields, compare
        d.text, d.order = text, order
        d.shown = shown or text
        return cls
    return apply


def custom(name: str, source: str) -> Callable[[type], type]:
    """Cython this emitter cannot derive, carried verbatim.

    The escape hatch, and it is counted. `_cpp/README` makes the same
    bargain for C++: a hatch nobody measures becomes the place the
    real code lives. The emitter reports how many lines went through
    here, so growth is visible rather than gradual."""
    def apply(cls: type) -> type:
        _decl(cls).custom[name] = source
        return cls
    return apply


def cxx_name[F: Callable[..., Any]](name: str) -> Callable[[F], F]:
    """What C++ calls this method, when it is not what Python does."""
    def apply(fn: F) -> F:
        fn._cxx_name = name  # type: ignore[attr-defined]
        return fn
    return apply


def blocks[F: Callable[..., Any]](fn: F) -> F:
    """This method can wait, so the emitter releases the GIL around it.

    Per-method rather than per-class, because a class whose calls
    mostly block still has accessors that cannot."""
    fn._blocks = True  # type: ignore[attr-defined]
    return fn


def reads[F: Callable[..., Any]](member: str) -> Callable[[F], F]:
    """This accessor reads a C++ DATA MEMBER, not a method.

    The distinction is not pedantry, it decides what gets emitted. A
    member read binds as `def_ro("name", &Cls::member)` and nanobind
    writes the accessor itself; a method call needs a lambda or a
    method pointer. `@cxx_name` says what C++ calls a FUNCTION, this
    says which FIELD is behind a name.

    On the Cython side the same fact reaches the pxd as a field
    declaration rather than a method, so one word serves both."""
    def apply(fn: F) -> F:
        fn._reads = member  # type: ignore[attr-defined]
        return fn
    return apply


def cxx_body[F: Callable[..., Any]](source: str) -> Callable[[F], F]:
    """The C++ this accessor cannot be derived into, carried verbatim.

    Per-method, and per-BACKEND: the body is C++, so it means nothing
    to the Cython emitter, which has `@custom` for the same job in its
    own language. A hatch that pretended to be portable would be
    lying about the one thing it exists to carry.

    Counted and printed, like `@custom`. `nix::ValidPathInfo` renders
    a store path against its own store directory, and that rendering
    is real logic rather than a binding - so it comes through here and
    shows up in the count."""
    def apply(fn: F) -> F:
        fn._cxx_body = source  # type: ignore[attr-defined]
        return fn
    return apply


def cxx_parts[F: Callable[..., Any]](
        prelude: str, **fields: str) -> Callable[[F], F]:
    """Where C++ finds each part of the value this call returns.

    The shape a produced value crosses in is a POD struct, and none of
    that shape is a decision: the struct, its members, the pxd that
    declares it and the Cython that unpacks it all follow from the
    fields the value declares. So the emitter writes them, and this
    carries the ONE thing it cannot know - which C++ expression yields
    each part.

    `prelude` is the call itself, as one or more statements. Each
    keyword names a declared field and gives C++ that evaluates to the
    wire type of that field: a `std::string` for a store path or a
    string, the width itself for an integer, a `std::vector` for a
    list. An optional field says the empty string when it is absent,
    which is the sentinel the emitter's Cython reads back.

    Every field must be named. A missing one is a struct member with
    nothing in it, which C++ would zero-initialise and Python would
    then read as a real answer."""
    def apply(fn: F) -> F:
        fn._cxx_parts = (prelude, dict(fields))  # type: ignore[attr-defined]
        return fn
    return apply


def needs[F: Callable[..., Any]](*headers: str) -> Callable[[F], F]:
    """C++ headers this method's body needs, beyond its class's.

    `@header` on a class names where the bound TYPE is declared, and
    that is the one header every method has in common. A body reaches
    further: `real_path` casts to nix::LocalFSStore, `add_to_store`
    builds a nix::StringSource, and neither type appears anywhere in
    the signature for an emitter to derive from.

    So the declaration says them. Not a list to keep in step with
    anything - a body that stops using a type stops naming it - and
    without it an emitted module fails to compile with a message
    about a type nobody can find the declaration for."""
    def apply(fn: F) -> F:
        fn._needs = headers  # type: ignore[attr-defined]
        return fn
    return apply


def instant[F: Callable[..., Any]](fn: F) -> F:
    """This method cannot wait, on a class whose calls generally can.

    The inverse of `@blocks`, and both are needed for the same reason:
    `blocking` is a property of a CLASS's calls in general, and a
    general rule has exceptions in both directions. nix::Store talks
    to a daemon, so `blocking=True` is right for it - and
    `get_store_dir` reads a string the config already holds, so
    releasing the GIL around it would cost two thread-state
    transitions to save nothing."""
    fn._instant = True  # type: ignore[attr-defined]
    return fn


def threading(policy: str) -> Callable[[F], F]:
    """Opt one FREE function into the generated surface, under `policy`.

    The marker is what opts a function in. An undeclared function is
    still part of the module and still described by the stubs; it just
    gets no async form and no rpc, which is what declaring nothing
    means - `gc_release_thread` is runtime plumbing and says so by
    carrying none.

    "pool" is the only policy that means anything here. A free
    function has no instance, so there is no thread for it to be
    affine to. A class says the same thing through `@binding`, where
    "affine" is a real answer."""
    if policy != "pool":
        raise ValueError(
            f"a free function may only declare 'pool' threading (got "
            f"{policy!r}). It has no instance, so there is no thread for "
            f"it to be affine to.")

    def apply(fn: F) -> F:
        fn._policy = policy  # type: ignore[attr-defined]
        return fn
    return apply


def binds[F: Callable[..., Any]](name: str) -> Callable[[F], F]:
    """The C++ function this free function binds.

    A class says `@binding(cxx=...)` because it maps onto a type. A
    free function maps onto a function, and usually one written by
    hand: nanopynix's `open_store` names `open_store_uri`, a helper
    that keeps a per-state-directory cache because two LocalStores in
    one process deadlock on a temp-roots flock.

    That helper is real logic and stays hand-written C++. What the
    declaration owns is the BINDING - the Python name, the parameter
    names, whether the call can wait - and naming the helper is how it
    reaches one without pretending to have written it."""
    def apply(fn: F) -> F:
        fn._binds = name  # type: ignore[attr-defined]
        return fn
    return apply
