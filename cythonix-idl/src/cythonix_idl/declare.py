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
from typing import Annotated, Any


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


def binding(cxx: str = "", threading: str = "pool",
            blocking: bool = True) -> Callable[[type], type]:
    """The C++ class this binds, and how it may be called.

    `blocking=False` means no method here can wait: every one is a
    read of memory the object already owns. It decides whether the
    emitter writes `with nogil:` and whether anything above needs a
    thread to hop to."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.cxx, d.threading, d.blocking = cxx, threading, blocking
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
