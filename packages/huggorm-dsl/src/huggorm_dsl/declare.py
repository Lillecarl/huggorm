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

import datetime
import os
import pathlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


class Threading(StrEnum):
    """Which thread a bound object's calls run on."""

    # Any pool thread.
    POOL = "pool"
    # The thread that made the object, and no other.
    AFFINE = "affine"


class Crossing(StrEnum):
    """How a bound object crosses a boundary."""

    # Behind a handle: the object stays where it lives.
    PROXY = "proxy"
    # Whole, as a copy rebuilt from its parts: `@wire_value`.
    VALUE = "value"
    # Never: the class has no async or remote surface. `@in_process`.
    LOCAL = "local"


class DeclKind(StrEnum):
    """What sort of declaration a `Decl` is."""

    # Binds a C++ type, or holds a produced value's slots.
    CLASS = "class"
    # A vocabulary: a StrEnum whose members ARE the strings a Nix
    # parser takes, with no C++ object behind it at all.
    WORDS = "words"
    # A sum of other declared types, written as an alias and named on
    # the wire.
    UNION = "union"
    # A declared exception.
    ERROR = "error"


@dataclass(frozen=True)
class Cxx:
    """How a Python type is spelled in C++.

    `copy` says what the boundary owes it. "view" means the value
    points into storage this binding does not own, so it is copied
    before it reaches Python - a view outliving its owner is a
    dangling pointer, not an exception.

    `width` is the wire scalar for an integer a sint64 cannot hold:
    `uint` for a uint64_t. Python has one `int`, so only the crossing
    needs it, and a service parameter cannot say it (huggorm#79)."""

    spelling: str
    copy: str = "value"
    width: str = ""


@dataclass(frozen=True)
class Async:
    """How a Python type is spelled on an ASYNC surface.

    `Cxx` says how a word is spelled below the boundary. This says
    how it is spelled above one: `pathlib.Path` blocks when it
    touches the disk, and `anyio.Path` is the same value with
    awaitable methods, so a caller already in an event loop can read
    the file without stopping it.

    A property of the WORD, beside its C++ spelling, because that is
    what it is. It used to be `_async_twins = {"pathlib.Path":
    "anyio.Path"}` in the bindings package, keyed by a type spelling
    - which put a fact about `Path` in a file that never names it.

    It never reaches the wire: a word crosses as its
    `wiretypes.SPELLED` builtin. It decides the annotation on every
    async surface - the protocol, the in-process wrapper and the RPC
    client - and the constructor call that wraps the answer. The
    reader puts it on the leaf `Type`, so it holds through `| None`
    and a list."""

    spelling: str


# The Nix this build links, as a tuple a declaration can compare.
#
# The generator sets it before it reads anything. A declaration that
# spans two Nix versions writes an ordinary `if` against it:
#
#     if NIX_VERSION >= (2, 34):
#         def get_uri(self) -> Str:
#             """How this store describes itself."""
#             Cxx("return self.config.getHumanReadableURI();")
#
# Python evaluates that during the import. Nothing in this package
# interprets a version condition, which is the whole reason the
# declaration is imported as well as parsed: the interpreter is
# already there and it is better at this than we would be.
#
# A build sets `HUGGORM_NIX_VERSION` to the version of the Nix it
# links (`2.35.2`, `2.36.0pre20260101_abcdef`), and only the major and
# minor count: a patch release changes no API. A value with no
# `major.minor` refuses the import, because a wrong arm is a binding
# that does not compile or, worse, one that does.
#
# Unset, it is what a bare `import huggorm_decl.decl.store` sees - a
# reader, an editor, a typechecker - so it is a real version rather
# than a sentinel that makes every comparison false.
def _nix_version() -> tuple[int, int]:
    raw = os.environ.get("HUGGORM_NIX_VERSION", "")
    if not raw:
        return (2, 34)
    found = re.match(r"(\d+)\.(\d+)", raw)
    if found is None:
        raise ValueError(f"HUGGORM_NIX_VERSION is {raw!r}, and it needs major.minor")
    return (int(found[1]), int(found[2]))


NIX_VERSION: tuple[int, int] = _nix_version()

# The same fact as names, because a type checker holds a NAME constant
# (`--always-true NIX_2_35`) and never a comparison. The declarations
# and the suite branch on these, so each is typechecked once per Nix.
NIX_2_35: bool = NIX_VERSION >= (2, 35)
NIX_2_36: bool = NIX_VERSION >= (2, 36)


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
U64 = Annotated[int, Cxx("uint64_t", width="uint")]
I64 = Annotated[int, Cxx("int64_t")]
# A Nix float is a C++ double (`NixFloat`), and so is a Python float.
F64 = Annotated[float, Cxx("double")]
# A std::string at the boundary and a pathlib.Path above it. Not the
# same as `Str`, and the difference is the whole point of the two:
# `print_store_path` answers in the STORE's terms, which may name a
# directory this machine does not have, and `real_path` answers where
# the bytes are here. Only the second is a path a caller can open.
Path = Annotated[pathlib.Path, Cxx("string"), Async("anyio.Path")]
# A SPAN of time, and the resolution is the fact this states.
# Upstream keeps a build's CPU time as `std::chrono::microseconds`, and
# nanobind's own chrono caster hands one to Python as a
# datetime.timedelta - so the caller gets Python's own duration type
# and this binding writes no conversion at all.
#
# Not the same as I64, and `start_time` beside `cpu_user` is the pair
# that shows why: a Unix time is a POINT, which is an integer and
# means nothing without an epoch, while a span is a quantity a caller
# can add up. Declaring both as `int` would have made them look alike.
Duration = Annotated[datetime.timedelta, Cxx("microseconds")]
# A Python callable the BINDING keeps and calls back.
#
# The only type here that does not describe a C++ value. `nb::object`
# is a reference to a Python object, so a parameter spelled this way
# says the C++ on the far side re-enters the interpreter - which is
# `EvalState.register_primop` and nothing else today.
#
# It cannot cross a wire, and `ir.NOT_DATA` says so there.
# That is not a gap to fill later: a remote client registering a
# primop would make the evaluator call BACK over the socket, on its
# own evaluation thread, once per invocation - a distributed call in
# a hot loop. huggorm#33 records it as a decision rather than an
# omission.
#
# `object` and not `Callable[..., Any]`, and the reflection gate is
# what settled it. `nb::object` accepts ANY Python object and nanobind
# reports the parameter as `object`, so a `Callable` annotation would
# promise a check the binding does not make - and a declaration may
# not be more permissive OR more specific than the C++ it binds. The
# docstring is where "this must be callable" belongs.
PyFunc = Annotated[object, Cxx("nb::object")]


@dataclass(frozen=True)
class Wrap:
    """How a C++ type holds a value the Python surface names directly.

    Two shapes need this, and they are the same fact. Upstream's
    opaque variant arm is `DerivedPathOpaque`, a struct whose only
    member is a `nix::StorePath`, and Python is given the StorePath.
    Upstream's `nix::ContentAddressMethod` is a struct whose only
    member is a `Raw` enum, and Python is given the word.

    Either way the type C++ hands over and the type the declaration
    names are different, and this is the one fact that says how to
    get from one to the other.

    `cxx` is the holder's own type, and `holds` is the member inside
    it. Together they are both directions: reading takes the member,
    writing builds the struct round it."""

    cxx: str
    holds: str


@dataclass(frozen=True)
class Enumerated:
    """The C++ enum a vocabulary's words stand for.

    A vocabulary is a StrEnum because a member IS the string libstore
    parses, and that stays true. This says what libstore holds once it
    has parsed one, which is the fact that makes the list CHECKABLE:
    the emitted conversion is a switch over `cxx` with no `default`,
    so a compiler with `-Werror=switch` refuses to build the day
    upstream adds an enumerator, and naming each one refuses the day
    upstream removes or renames one.

    `cxx` is the enum's own type. `spelled` names any word whose
    enumerator is spelled differently - `NAR` is `NixArchive`
    upstream - by the word's own name. A word not named here is
    `{cxx}::{word}`, which is every word in the usual case.

    Not an `nb::enum_`. Measured (huggorm#70): a chain of
    `.value("MD5", ...)` calls with an enumerator left out compiles
    silently, because `.value` is a runtime call and there is nothing
    for the compiler to check it against. A switch is the whole
    difference.
    """

    cxx: str
    spelled: dict[str, str] = field(default_factory=dict)
    # The struct C++ hands over, when it does not hand the enum over
    # bare. `nix::ContentAddressMethod` is a struct whose only member
    # is the `Raw` enum, and a method that answers one answers the
    # struct - so the conversion takes that and reaches inside.
    wrapped: Wrap | None = None

    @property
    def held(self) -> str:
        """The C++ type a conversion of this TAKES.

        The enum itself in the usual case, and the struct around it
        where upstream wraps one."""
        return self.wrapped.cxx if self.wrapped else self.cxx

    @property
    def reach(self) -> str:
        """How to get from that type to the enum. Empty when it IS one."""
        return f".{self.wrapped.holds}" if self.wrapped else ""

    def enumerator(self, word: str) -> str:
        """The C++ enumerator for one word, by the word's own name."""
        return f"{self.cxx}::{self.spelled.get(word, word)}"


@dataclass(frozen=True)
class Variant:
    """The C++ union behind a declared sum type.

    A union is a TYPE, so its C++ facts ride on the alias rather than
    on a decorator - which is what this module's own header says
    Annotated aliases are for. There is nothing else to put them on:
    `A | B` is an expression, and a class whose BASES were the arms
    would say the opposite of what a sum type means. Inheritance is
    "is a", so `class DerivedPath(StorePath, DerivedPathBuilt)` makes
    a DerivedPath a StorePath, when the truth runs the other way -
    and `read.py` refuses a base on a bound class anyway.

    `cxx` is the union's own C++ type. `raw` is how to reach the
    std::variant inside it: upstream's unions PUBLICLY INHERIT their
    variant and re-expose it through `raw()`, so a visit says
    `std::get_if<...>(&p.raw())` rather than `&p`.

    `wraps` names any arm the variant does not hold directly, by the
    declared arm's name. An arm not named here is held as itself.

    `header` is where the union's own type is declared. A union has
    no `@header` decorator to carry it, and the translation unit that
    only PASSES one - `store.cpp` takes a DerivedPath and declares
    none of its arms - would otherwise name a type it never included.

    `bare` says the union's C++ type IS `std::variant` of the arms, in
    the declared order, as `nix::StoreReference::Variant` is. nanobind's
    own caster then casts it, and a caster of ours would specialise the
    type it delegates to. So nothing is emitted for it but a
    `static_assert` that holds the claim.
    """

    cxx: str
    raw: str = ""
    header: str = ""
    wraps: dict[str, Wrap] = field(default_factory=dict)
    bare: bool = False


@dataclass(frozen=True)
class Field:
    """One declared part of a wire value.

    `read` names the accessor that produces it, because the field name
    and the accessor need not agree: a StorePath's part is called
    `base_name` and is read by `to_string`. The type is that
    accessor's annotation, so a field does not state one."""

    name: str
    read: str


@dataclass
class Decl:
    """Everything the emitter knows about one bound class."""

    name: str = ""
    header: str = ""
    # Headers this class's BODIES need, beyond the one above, from a
    # class-level `@needs`. A method may say it for itself; a class
    # says it once when several of its bodies reach the same place.
    headers: tuple[str, ...] = ()
    cxx: str = ""
    # Nothing a caller writes builds one: `@produced`.
    produced: bool = False
    # The free function bound as the constructor: `@constructs(cls)`.
    factory: str = ""
    threading: Threading = Threading.POOL
    blocking: bool = True
    wire: Crossing = Crossing.PROXY
    # The declared class this one derives from, or "". Only an
    # `@in_process` class has one: no async or remote surface carries
    # a hierarchy.
    base: str = ""
    # Each part either a `Field` or the NAME of the accessor that
    # answers it. See `wire_value`.
    fields: tuple[Field | str, ...] = ()
    # The arms of a UNION, by declared name. A union is written as a
    # module-level alias - `DerivedPath = StorePath | DerivedPathBuilt`
    # - so it has no decorator to carry this and the reader fills it
    # in. Empty for everything that is not one.
    arms: tuple[str, ...] = ()
    # The arms that cross as a builtin, by their name in `arms`, which
    # is the wire spelling: `uint` for a U64. Each value is the reader's
    # `Type` for the arm, so an emitter spells its C++ from the alias
    # the declaration wrote. `Any` because the reader imports this
    # module, not the other way round.
    scalars: dict[str, Any] = field(default_factory=dict)
    # The C++ union a sum type stands for, from the `Variant(...)` on
    # its Annotated alias. None for a union declared without one,
    # which is legal: a union whose arms C++ holds directly needs no
    # conversion written at all.
    variant: Variant | None = None
    compare: str = ""
    text: str = ""
    shown: str = ""
    order: bool = False
    # A value with no parts. See `wire_value`.
    unit: bool = False
    custom: dict[str, str] = field(default_factory=dict)
    # A `PyType_Slot[]` this class's binding installs, by C++ name.
    # Empty for everything that holds no Python object. See `gc_slots`.
    gc_slots: str = ""
    kind: DeclKind = DeclKind.CLASS
    # Where the words go, for a vocabulary. The emitter writes this
    # call at each site that takes one, so an experimental word is
    # refused by upstream rather than accepted by us.
    parsed_by: str = ""
    # The C++ enum a vocabulary's words stand for, from `@words`.
    # None for a vocabulary with no enum behind it, which gets the
    # Python surface and no check. See `Enumerated`.
    enumerated: Enumerated | None = None
    # Whether Python may construct one. An abstract base still gets a
    # class, an async wrapper and a wire identity - a caller holds a
    # base most of the time - but calling it would build an
    # object with no implementation behind it.
    abstract: bool = False
    # How a value TREE is walked, for a type that holds others. Read
    # by the RPC layer, so no layer above the declaration knows what
    # the type is or which of its methods do what.
    tree: Tree | None = None
    # How Python DATA becomes one of the tree's values, on a type that
    # makes them. Read by the RPC layer, as `tree` is.
    builds: Builds | None = None
    # What Python holds one of these THROUGH. Empty for the usual
    # case, where Python owns the object outright. "shared_ptr" for a
    # class whose factory hands back a reference-counted handle -
    # nix::openStore does, and a store has to stay open for as long
    # as the object naming it does.
    holder: str = ""
    # How to reach the object whose methods this class binds, when the
    # bound type is a HANDLE rather than the object itself. Empty for
    # everything that binds its own methods; "get()" for a wrapper
    # that exists to own a lifetime - `huggorm::Bridge` roots a
    # GC-resident value and hands it back through `get()`.
    #
    # One fact, three derivations. A method calls through it, a
    # return of this class wraps in it, and a parameter of this class
    # unwraps out of it - which is every mechanical line such a
    # binding used to carry verbatim.
    via: str = ""
    # The C++ type libstore uses for a COLLECTION of these.
    #
    # `list[StorePath]` crosses the wire as a list and reaches
    # libstore as a `nix::StorePathSet`, which is a fact about the
    # element type rather than about any one method - so it is stated
    # here once instead of at a dozen call sites. Empty means a plain
    # vector, which is what a caster already gives.
    collection: str = ""
    # This class is a handle over a TAGGED UNION. See `@tagged`.
    # Not `arms` above: that is a SUM TYPE's alternatives, which is
    # a Python-level union. This is one C++ object with a tag.
    #
    # (reach, ask, {enumerator: name}). `reach` hands the union over -
    # `get()`. `ask` says which arm it holds - `type<true>()`. The map
    # is every arm the union has, and the name each one answers to.
    #
    # Separate from `via`, and deliberately. `via` routes EVERY method
    # through the held object, which is wrong here - most of this
    # handle's methods are its own, because a union accessor needs
    # state the value does not carry. Only a `@guard`ed accessor
    # reaches through.
    tagged: tuple[str, str, dict[str, str]] | None = None


# -- what each marker means, as DATA --------------------------------
#
# Where a marker is legal, how many times, and what it conflicts with.
# One table, read by `read.py` so a rule is CHECKED rather than
# restated in a docstring and enforced nowhere (huggorm#61).
#
# Before this, `read.py` carried 28 hand-written raises and the rules
# below were prose. The ones that were never written stayed unchecked:
# `@needs` on a class worked by accident before it worked on purpose,
# and `@local` on a class that is not a wire value is still harmless
# and silent.


@dataclass(frozen=True)
class Marker:
    """One decorator's rules.

    `target` is which kinds of thing it may decorate: a class, a
    method of one, or a module-level function.

    `arity` is "flag" for a bare `@name`, "once" for a `@name(...)`
    that may appear a single time, and "repeatable" for one that may
    stack.

    `excludes` names markers it cannot appear beside. `requires` names
    ones it needs - empty today, and kept because `@pure` needed
    `@virtual` before both were deleted, so the shape recurs.
    """

    target: frozenset[str]
    arity: str
    excludes: frozenset[str] = frozenset()
    requires: frozenset[str] = frozenset()


def _t(*targets: str) -> frozenset[str]:
    return frozenset(targets)


# The table is SELF-ENFORCING. `_check_markers` rejects any decorator
# name without a row here, so a marker added below and not added here
# cannot be used by a declaration at all - it fails on first use, with
# a "did you mean" suggestion, rather than working silently under no
# rules.
MARKERS: dict[str, Marker] = {
    # On a class.
    "abstract": Marker(_t("class"), "flag"),
    "tagged": Marker(_t("class"), "once"),
    "binding": Marker(_t("class"), "once"),
    "builds": Marker(_t("class"), "once"),
    "custom": Marker(_t("class"), "once"),
    "gc_slots": Marker(_t("class"), "once"),
    "header": Marker(_t("class"), "once"),
    "in_process": Marker(_t("class"), "flag",
                         excludes=frozenset({"wire_value"})),
    "produced": Marker(_t("class"), "flag"),
    "tree": Marker(_t("class"), "once"),
    "wire_value": Marker(_t("class"), "once"),
    "words": Marker(_t("class"), "once"),
    # On a method, or on a module-level function.
    "binds": Marker(_t("method", "free"), "once"),
    "blocks": Marker(_t("method", "free"), "flag",
                     # Documented inverses: one says a call can wait,
                     # the other says it cannot.
                     excludes=frozenset({"instant"})),
    "cxx_name": Marker(_t("method"), "once"),
    "spells": Marker(_t("method"), "once"),
    "fills": Marker(_t("method"), "once"),
    "guard": Marker(_t("method"), "once"),
    "names": Marker(_t("method"), "flag"),
    "produces": Marker(_t("method"), "once"),
    "instant": Marker(_t("method"), "flag",
                      excludes=frozenset({"blocks"})),
    "local": Marker(_t("method"), "flag"),
    "reads": Marker(_t("method"), "once"),
    "wire_read": Marker(_t("method"), "once"),
    "threading": Marker(_t("method", "free"), "once"),
    # On anything that names C++ it needs compiled beside it.
    "needs": Marker(_t("class", "method", "free"), "repeatable"),
    # On a module-level function only.
    "constructs": Marker(_t("free"), "once"),
    "startup": Marker(_t("free"), "flag"),
    "translator": Marker(_t("free"), "flag"),
    # On a method of an @in_process class.
    "virtual": Marker(_t("method"), "flag"),
}


def in_process(cls: type) -> type:
    """This class never crosses: no async wrapper, no protocol, no RPC.

    For a class whose objects only make sense in this process, such
    as a base a Python subclass implements for Nix to call. A handle
    to one would name an object no remote caller can use."""
    _decl(cls).wire = Crossing.LOCAL
    return cls


def abstract(cls: type) -> type:
    """Python may not construct one of these.

    The base of a hierarchy whose leaves are the implementations. It
    still gets a class, because a caller holds the base far more
    often than a leaf - what it does not get is a constructor."""
    _decl(cls).abstract = True
    return cls


@dataclass(frozen=True)
class Leaf:
    """A node that crosses as one scalar. `wire` is its declared type,
    which picks the arm of the value message; `read` is the accessor
    that answers it."""

    wire: str
    read: str


@dataclass(frozen=True)
class Null:
    """A node that crosses as None. It has nothing to read."""


@dataclass(frozen=True)
class Items:
    """A node that holds values by position: `size` counts them and
    `item(i)` reads one."""

    size: str
    item: str


@dataclass(frozen=True)
class Entries:
    """A node that holds values by name: `size` counts them, and
    `name(i)` and `value(i)` read one."""

    size: str
    name: str
    value: str


@dataclass(frozen=True)
class Tree:
    """How a value tree is walked. `kind` names the accessor that says
    what a node is, and `kinds` maps each answer to how that node is
    read. An answer `kinds` does not hold crosses as a proxy.

    `identity` names the accessor that makes two nodes the same node.
    Empty means Python identity.

    `force` names the accessor that forces a node in place, for a walk
    that forces. `stop` names a predicate on an `Entries` node that
    keeps it a proxy in such a walk: a derivation, which a forcing walk
    must not enter."""

    kind: str
    kinds: dict[str, Leaf | Items | Entries | Null]
    identity: str = ""
    force: str = ""
    stop: str = ""


def tree(kind: str, kinds: dict[str, Leaf | Items | Entries | Null],
         identity: str = "", force: str = "",
         stop: str = "") -> Callable[[type], type]:
    """How a value TREE is walked, for a type that holds others.

    The RPC layer reads it, so no layer above this declaration knows
    what the type is or which of its methods do what. The build checks
    every accessor it names against the class, and the emitter writes
    it into `_policy.TREES`, which the server reads."""
    spec = Tree(kind, kinds, identity, force, stop)

    def apply(cls: type) -> type:
        _decl(cls).tree = spec
        return cls
    return apply


@dataclass(frozen=True)
class Builds:
    """How Python data becomes a value, by the methods that make one.

    `null` takes no argument. `leaves` maps a tree leaf's wire type to
    the method that makes one from that scalar. `items` makes an empty
    list and `add_item(list, value)` appends to it; `entries` and
    `add_entry(attrs, name, value)` do the same for an attribute set."""

    null: str
    leaves: dict[str, str]
    items: str
    add_item: str
    entries: str
    add_entry: str


def builds(null: str, leaves: dict[str, str], items: str, add_item: str,
           entries: str, add_entry: str) -> Callable[[type], type]:
    """How Python data becomes a value, for a type that makes values.

    The RPC layer reads it and builds a whole value in one hop, so no
    layer above this declaration names a builder. The build checks
    every method it names against the class, and the emitter writes it
    into `_policy.BUILDERS`."""
    spec = Builds(null, leaves, items, add_item, entries, add_entry)

    def apply(cls: type) -> type:
        _decl(cls).builds = spec
        return cls
    return apply


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


def produced(cls: type) -> type:
    """This class is built by something else, never constructed.

    A produced value holds no C++ object at all: the object that made
    it flattened one, and what is left is Python slots. So the shape
    is different from a bound class rather than a variation on it -
    no header, no pointer, an __init__ that raises, and a _from_parts
    that fills a __new__ instance because there is no constructor to
    call.

    What makes one is read off the return types, and the refusing
    __init__ names each such call, so a caller who guesses wrong is
    told where to look."""
    _decl(cls).produced = True
    return cls


def constructs(cls: type) -> Callable[[F], F]:
    """This free function is `cls`'s constructor.

    `open_store` builds a Store, so a caller writes `Store("auto")`
    and the function is bound as the class's `__new__`, never as a
    name of its own. The function owns the signature; the class's
    `__init__` holds only the prose.

    On the function, not the class: the class is defined first, so
    the function names an object that exists, where the class could
    only name the function by a string."""
    def apply(fn: F) -> F:
        _decl(cls).factory = fn.__name__
        return fn
    return apply


def words(parsed_by: str = "", enumerated: Enumerated | None = None,
          ) -> Callable[[type], type]:
    """This class is a VOCABULARY: the words a Nix parser takes.

    A StrEnum, so a member IS the string libstore parses. Passing
    `ContentAddressMethod.FLAT` and passing `"flat"` are the same
    call, which is what keeps such a class a convenience rather than
    a layer - it names what libstore already accepts, so an editor
    can offer the words and a typo fails before the call.

    The Python surface is plain: no pointer, no header, no shim. What
    goes TO libstore is the string, handed to `parsed_by`.

    `parsed_by` names the C++ that takes them. It is a call the
    emitter writes at each site rather than prose: upstream's parser
    is the only thing that knows a word is behind an experimental
    feature, and a binding that mapped the string itself would accept
    `blake3` where libstore refuses it.

    `enumerated` names the C++ enum the words stand for, when there is
    one. It buys the direction `parsed_by` does not have - a word
    coming BACK from libstore - and, because that direction is a
    switch, it makes the compiler check the list."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.kind, d.parsed_by, d.enumerated = DeclKind.WORDS, parsed_by, enumerated
        return cls
    return apply


def gc_slots(table: str) -> Callable[[type], type]:
    """This class holds Python objects, and here is how to traverse
    them.

    `table` names a `PyType_Slot[]` in C++, and the emitted
    `nb::class_` passes it as `nb::type_slots(...)`. That table
    supplies `Py_tp_traverse` and `Py_tp_clear`.

    WHY a C++ symbol rather than something the DSL spells: a traversal
    is a function the interpreter calls during collection, over a
    member no declaration knows about. There is nothing here to
    derive it from, and nanobind offers no abstraction either - its
    `refleaks.rst` says to drop to the CPython slots.

    So this is the shape a helper should have. The declaration decides
    that the class needs slots and names them; the helper supplies
    them; generated code is what installs them.

    A class that stores a Python callable and does NOT say this leaks
    itself whenever that callable closes over it, which is the normal
    way to write one (huggorm#93). Nothing detects the omission
    today."""
    def apply(cls: type) -> type:
        _decl(cls).gc_slots = table
        return cls
    return apply


def binding(cxx: str = "",
            threading: Literal["pool", "affine"] = "pool", holder: str = "",
            blocking: bool = True, via: str = "",
            collection: str = "") -> Callable[[type], type]:
    """The C++ class this binds, and how it may be called.

    `blocking=False` means no method here can wait: every one is a
    read of memory the object already owns. It decides whether the
    emitter writes `with nogil:` and whether anything above needs a
    thread to hop to.

    `holder` is what Python holds one THROUGH, and it is empty for
    almost everything. "shared_ptr" is for a class whose factory
    hands back a reference-counted handle: nix::openStore does, and a
    store has to stay open for as long as the object naming it
    does.

    `via` is for the other kind of indirection: `cxx` names a HANDLE
    rather than the object itself, and `via` is how the handle hands
    that object over. `via="get()"` makes every method call through
    it, wraps a return of this class in it and unwraps a parameter of
    this class out of it. A method that belongs to the HANDLE rather
    than to what it points at says so with its own `@cxx_body`, and
    the census counts it - which is the honest split, because those
    are the only lines that are about the handle at all."""
    def apply(cls: type) -> type:
        d = _decl(cls)
        d.cxx, d.blocking = cxx, blocking
        d.threading = Threading(threading)
        d.holder, d.via, d.collection = holder, via, collection
        return cls
    return apply


def wire_value(fields: tuple[Field | str, ...] = (), compare: str = "parts",
               text: str = "", order: bool = False,
               shown: str = "", unit: bool = False,
               ) -> Callable[[type], type]:
    """This class serializes, and here is what it is made of.

    `unit` says the value has NO parts, and that is the whole fact:
    `DerivationOutput::Deferred` is an arm of a union that carries
    nothing. The build refuses a value with no parts unless it says
    this, because the usual reason for none is a forgotten field.

    `fields` says SELECTION and ORDER: which accessors are parts, and
    which position each takes in the message. A plain NAME is the
    common case - the part is called what the accessor is called, and
    its type is the annotation the accessor already carries, so
    nothing here restates a type. `Field(...)` is for the other case,
    where the two names differ: a StorePath's part is `base_name` and
    is read by `to_string`.

    A class that lists none crosses as EVERY accessor it has, in
    declaration order. That is the common case and it restates
    nothing: the accessors are already there, one screen above.

    An accessor that must be kept OFF the wire says so itself, with
    `@local`. `Hash` is why: it carries the two facts a hash IS and
    three ways to print them, and a rendering derived from fields
    already crossing would be the same bytes a second time.

    ORDER is declaration order, which means REORDERING accessors for
    readability moves wire positions. Free while the two sides are
    built together (055); when 022's lockfile pins field numbers, it
    inherits this and a reorder becomes a schema change.

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
        d.wire, d.fields, d.compare = Crossing.VALUE, fields, compare
        d.text, d.order = text, order
        d.shown = shown or text
        d.unit = unit
        return cls
    return apply


def custom(name: str, source: str) -> Callable[[type], type]:
    """Whole-class C++ this emitter cannot derive, carried verbatim.

    The escape hatch, and it is counted. `cpp/README` makes the same
    bargain: a hatch nobody measures becomes the place the real code
    lives. The emitter reports how many lines went through here, so
    growth is visible rather than gradual.

    `@cxx_body` is the per-METHOD hatch, and it is the one to reach
    for first. This carries lines a class needs and no method owns."""
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


def tagged(reach: str, ask: str, **names: str) -> Callable[[type], type]:
    """This class is a handle over a TAGGED UNION.

    `reach` hands the union over. `ask` says which arm it holds. The
    keyword arguments are the arms: a NAME, and the C++ enumerator it
    stands for.

    One table, two derivations. Every `@guard`ed accessor looks its
    arm up here, and the accessor that ANSWERS the name - `@names` -
    is the same table read the other way. So adding an arm is one line
    rather than a switch case and twelve guards to keep in step.

    Written name-first because the name is the Python fact and the
    enumerator is the C++ one, and a declaration reads better in the
    direction it is used."""
    def mark(cls: type) -> type:
        _decl(cls).tagged = (reach, ask, dict(names))
        return cls
    return mark


def produces[F: Callable[..., Any]](init: str) -> Callable[[F], F]:
    """This method makes a new value by calling ONE initialiser.

    `init` is the C++ name of that initialiser, and it is the whole of
    what this says. The emitter owns the rest - allocating the value
    on this state, calling the initialiser with the declared
    arguments, rooting it, and wrapping it in the handle - so a
    producer is one name here rather than four lines of C++ per
    producer.

    The `@reads` precedent, pointed the other way: that marker names a
    C++ member to READ, this one names a C++ initialiser to CALL.

    The moment `init` is not enough - an extra argument, an ordering,
    a conditional - the method is not in this pattern. Give it a body
    instead of stretching the marker, because a marker that describes
    structure is a C++ string with punctuation."""
    def mark(fn: F) -> F:
        fn._produces = init  # type: ignore[attr-defined]
        return fn
    return mark


def fills[F: Callable[..., Any]](maker: str, arm: str) -> Callable[[F], F]:
    """This method mutates a value the binding BUILT, and nothing else.

    `maker` is the method that makes such a value, and it is what the
    refusal names. `arm` is what the target must BE - a builder of the
    wrong kind passes the builder test and then reads the wrong union
    member, which is undefined rather than an error.

    The maker names - a caller who reaches this is told where to get a
    fillable value, not merely that this one is wrong.

    The rule is the library's, not this repo's. A Nix collection is
    immutable, so rewriting an evaluated one corrupts memory the state
    holds in caches and other handles point at. The builders are the
    one place where rewriting is safe, because nothing else has seen
    the value yet.

    A marker rather than two hand-written checks, because it repeats:
    the same decision covers appending to a list and setting an
    attribute, and a third builder would make it three."""
    def mark(fn: F) -> F:
        fn._fills = (maker, arm)  # type: ignore[attr-defined]
        return fn
    return mark


def names[F: Callable[..., Any]](fn: F) -> F:
    """This accessor answers which arm the union holds, by NAME.

    Emitted from the `@arms` table, so the names a caller sees and
    the names `@guard` checks against cannot drift apart."""
    fn._names = True  # type: ignore[attr-defined]
    return fn


def guard[F: Callable[..., Any]](arm: str) -> Callable[[F], F]:
    """This accessor is valid only when the union holds `arm`.

    Not politeness. `nix::Value`'s readers are `noexcept` and
    undefined on the wrong tag: reading `integer` off a string is not
    an error, it is a reinterpretation of the payload.

    `arm` is a NAME from the class's `@arms` table, and the emitter
    resolves it to the enumerator. Naming the enumerator here instead
    would put the same C++ fact in thirteen places.

    This is what makes a handle over a union bindable at all. A method
    bound by pointer cannot check anything first, which is why binding
    a tagged union that way is wrong. A generated body can check."""
    def mark(fn: F) -> F:
        fn._guard = arm  # type: ignore[attr-defined]
        return fn
    return mark


def blocks[F: Callable[..., Any]](fn: F) -> F:
    """This method can wait, so the emitter releases the GIL around it.

    Per-method rather than per-class, because a class whose calls
    mostly block still has accessors that cannot."""
    fn._blocks = True  # type: ignore[attr-defined]
    return fn


def local[F: Callable[..., Any]](fn: F) -> F:
    """This accessor is for a caller, and does not cross the wire.

    A wire value crosses as every accessor it has, so nothing lists
    them twice - and that makes this the one thing an accessor has to
    be able to say for itself. `Hash` carries the two facts a hash IS,
    the algorithm and the digest, and three ways to PRINT them; a
    rendering derived from fields already on the wire would be a
    fourth, fifth and sixth copy of the same bytes.

    The test is whether `_from_parts` could not rebuild the value
    without it. If it could, the accessor is a convenience and belongs
    here."""
    fn._local = True  # type: ignore[attr-defined]
    return fn


def virtual[F: Callable[..., Any]](fn: F) -> F:
    """A Python subclass may override this C++ virtual.

    The binding emits a trampoline. When C++ calls the method, a
    Python override answers; with none, the C++ implementation does,
    and an override reaches it with `super()`. The lookup holds the
    GIL and the C++ implementation runs without it. A declared Nix
    error the override raises reaches C++ as that error.

    Only on an `@in_process` class: an override is Python in this
    process, and a remote caller could not reach it. The method binds
    the C++ member by name, so it carries no body."""
    fn._virtual = True  # type: ignore[attr-defined]
    return fn


def reads[F: Callable[..., Any]](member: str,
                                 collection: str = "") -> Callable[[F], F]:
    """This accessor reads a C++ DATA MEMBER, not a method.

    The distinction is not pedantry, it decides what gets emitted. A
    member is reached - `self.narSize` - where a method is called, and
    `.def` takes a function so `&T::narSize` cannot be bound directly.
    `@cxx_name` says what C++ calls a FUNCTION, this says which FIELD
    is behind a name.

    One word, because which of the two it is is a fact about C++
    rather than about the surface: a caller writes `info.name()`
    either way.

    `collection` is the C++ type this member is, where a
    `list[T]` reaches a container that is not a vector.
    `@binding(collection=...)` states the same fact about an ELEMENT
    class - every `list[StorePath]` is a `nix::StorePathSet` - and
    this is for the case that has no element class to state it on:
    `nix::GCResults::paths` is a `StringSet`, and `str` is a builtin.

    Only the WRITE direction needs it. Reading goes through `as_list`,
    which is a template over any range."""
    def apply(fn: F) -> F:
        fn._reads = member  # type: ignore[attr-defined]
        fn._member_collection = collection  # type: ignore[attr-defined]
        return fn
    return apply


def wire_read[F: Callable[..., Any]](accessor: str) -> Callable[[F], F]:
    """This part crosses the wire as what `accessor` reads.

    The part keeps this accessor's name and position. Only the reader
    changes: `LogRecord.text` decodes strict UTF-8, so the wire reads
    `text_bytes` and a line in any other encoding still crosses.

    Said HERE rather than in a `wire_value(fields=...)` list, because a
    list restates every part to change one, and an accessor added later
    and left out of it silently stays off the wire. `accessor` must be
    `@local`, or it would cross a second time as a part of its own."""
    def apply(fn: F) -> F:
        fn._wire_read = accessor  # type: ignore[attr-defined]
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


def spells[F: Callable[..., Any]](*words: str) -> Callable[[F], F]:
    """Vocabularies this method's BODY spells, beyond its signature.

    Not `@names`, which is taken: that one says an accessor ANSWERS
    which arm of a tagged union is held.

    `@needs` for headers, and the same fact one level up. A body
    reaches further than a signature: `KeyedBuildResult.error` builds
    a Python exception carrying a failure word, and the word appears
    nowhere in `-> BuildError | None` for an emitter to derive from.

    Without it the emitted unit has no conversion for that vocabulary
    and fails to COMPILE, which is a real gate rather than a silence -
    but it fails naming a `huggorm::as_word` overload, a long way from
    the declaration that wanted it.

    NAMES, not the classes. A decorator argument here is a constant -
    a declaration holds facts, not expressions - and the class object
    is an expression the reader refuses. So the emitter checks
    instead: a name no declaration declares as an enum-backed
    vocabulary is refused rather than skipped, which is the gate the
    typed argument would have been."""
    def apply(fn: F) -> F:
        fn._spells = words  # type: ignore[attr-defined]
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


def threading(policy: Literal["pool"]) -> Callable[[F], F]:
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
        fn._policy = Threading(policy)  # type: ignore[attr-defined]
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
