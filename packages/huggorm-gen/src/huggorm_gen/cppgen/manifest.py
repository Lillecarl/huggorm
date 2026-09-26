"""
Declaration -> manifest entry.

This is the half of the idea that is not about a binding language at
all, and it is the half that decided whether the idea was worth
adopting.

## The ordering the manifest used to live under

`model.py` built the manifest by IMPORTING `huggorm_bindings` and
reflecting on the classes it found: `getattr(cls, "_threading")`,
`cls.__dict__["_binds"]`, `getattr(cls, d) is not getattr(object, d)`
for the dunders. Every one of those reads a COMPILED extension type.

So the build had one possible order. C++ compiled, the binding
compiled, the manifest was learnt, and only then could the async
wrappers, the protocols, the RPC stubs and the type stubs be written.
Four surfaces waited behind a C++ compiler for facts a person had
decided in a declaration before any of it started.

## What this module does instead

It reads the same facts out of the declaration, which is a text file.
`_binds` is "C" plus the class name. `_threading` is what `@binding`
said. The nine value dunders are what `@wire_value` implies: a value
compares, hashes and prints; `order=True` adds the four comparisons
`functools.total_ordering` fills in; `text=` adds `__str__`.

Every one of those is knowable before a compiler runs, because the
declaration is where the decision was made. Reflection was reading
back a fact that had been written down two files earlier.

## What made this a claim rather than a hope

A gate diffed what this emits against the entry the real build
produced by reflection. Equal meant the declaration carries the whole
manifest and reflection is redundant; anything else named the field
this route could not reach. The gate is gone because its other side
is: reflection has nothing to read in a nanobind class, and the
generator asks this module instead.
"""

import copy
import inspect
from collections.abc import Sequence
from typing import Any

from huggorm_dsl.declare import Decl
from huggorm_dsl.read import Class, Method, Param, Type

# The names a proxy's RPC surface is spelled with. Derived from the
# class name in every case, so the whole surface is knowable from a
# declaration - but the WORDS are the generator's, so they are named
# here rather than written into a format string five times.
PROTO_PACKAGE = "huggorm.v1"
SERVICE = "Service"
ACQUIRE = "Acquire"
PROTOCOL = "Like"
ASYNC = "Async"
RPC = "RPC"

# C++ spelling -> the Python type the manifest names. The manifest is
# read as Python by every layer above, so `bint` and `string_view`
# have no place in it: what a caller sees is `bool` and `str`.
PYTHON = {
    "string": "str",
    # A view is a str by the time it reaches Python: the binding
    # copies it, because a view outliving its owner is a dangling
    # pointer rather than an exception.
    "string_view": "str",
    "bint": "bool",
    # Widths. Python has one integer type, so both read as `int` here
    # - the width is a fact about the crossing, not about the value a
    # caller holds.
    "uint64_t": "int",
    "int64_t": "int",
    "double": "float",
    # A span, and Python has a type for one. Not `int`, which is what
    # the two widths above read as: a timedelta says what the number
    # MEANS, and nanobind's chrono caster hands one over already.
    "microseconds": "datetime.timedelta",
    # The one entry that does not describe a C++ VALUE. `nb::object`
    # is a reference to a Python object, so the Python spelling is
    # what the caller already had - nothing is marshalled in either
    # direction, and the binding holds the reference to call back
    # through (tasks/033).
    "nb::object": "object",
}

# What a wire value defines, and what makes it define each one. Read
# as: this dunder is present when this fact about the declaration is
# true. `model.VALUE_DUNDERS` is the same nine names on the other
# side of the boundary, found there by asking a compiled class.
DUNDERS: tuple[tuple[str, str], ...] = (
    # A value compares, hashes and prints as the thing it IS. Not
    # optional: model.REQUIRED_DUNDERS names the first three, and the
    # wire contract refuses a value type missing any of them.
    ("__eq__", "value"),
    # Python gives `!=` a slot of its own the moment `__eq__` exists,
    # so it is present by consequence rather than by choice.
    ("__ne__", "value"),
    ("__hash__", "value"),
    ("__repr__", "value"),
    # One declared comparison, plus the three functools.total_ordering
    # writes from it.
    ("__lt__", "order"),
    ("__le__", "order"),
    ("__gt__", "order"),
    ("__ge__", "order"),
    # Only when the declaration named an accessor worth printing.
    ("__str__", "text"),
)


def _type(t: Type | None) -> str:
    """The manifest's spelling of a declared type.

    A type with no C++ behind it is already Python and needs no
    table: a produced value's field crosses as itself.

    For one that does, this refuses rather than defaults. A spelling
    the table does not know is one the surfaces above cannot marshal,
    and inventing a name here would push the failure into an emitted
    file."""
    if t is None:
        return "None"
    leaf = t.leaf
    if leaf.cxx is None:
        return t.python
    if leaf.cxx.spelling not in PYTHON:
        raise TypeError(
            f"'{leaf.cxx.spelling}' has no Python spelling. Add it to "
            f"manifest.PYTHON once the boundary knows how to marshal it.")
    # The DECLARATION's spelling wins where the two disagree. A
    # std::string is a `str` most of the time, and is `bytes` or a
    # `pathlib.Path` where the alias says so - the table gives the
    # usual reading, and only the alias knows when it is not the one.
    return t.python if t.python != "bool" else PYTHON[leaf.cxx.spelling]


# The C++ spellings the manifest cannot carry across a SERVICE.
#
# A field says its own width - `_wire_fields` spells one `uint` and
# the other `int` - and a parameter does not: `params[].type` is one
# string, read by the stub emitter as a Python annotation AND by the
# schema builder as a wire type. `int` is right for the first and
# wrong for the second, and there is no second string to put the
# width in.
#
# Refused rather than carried, because nothing declares one today.
# The alternative is a second key beside `type`, which `model.py`
# cannot reflect off a compiled class - so `check.py` would diff the
# manifest against a shape reflection has no way to produce. Adding
# that for zero callers buys a bug, not a feature (tasks/079).
#
# This RAISES, so everything here is a GAP rather than a decision: a
# width is something the manifest could learn to say. A type that
# should NEVER cross belongs in `grpc_schema.NOT_DATA` instead, which
# REPORTS - the method then keeps its in-process wrapper and loses
# only the rpc, which is the shape `Store.real_path` already has.
#
# `nb::object` went here first and stopped the whole build, which is
# the wrong answer for something deliberate (tasks/033).
UNCROSSABLE = {
    "uint64_t": (
        "a service's message carries no width: every int parameter "
        "crosses as sint64, which holds half of one. Teach the "
        "manifest to spell a parameter's wire type - see tasks/079."),
}


def _crossable(t: Type | None, where: str) -> None:
    """Refuse a type a service's message cannot spell.

    Called for what a SERVICE carries - a proxy's methods and the
    parameters that acquire one - and not for a value's accessors. A
    value crosses as its fields, and a field says its width."""
    leaf = t.leaf if t is not None else None
    if leaf is not None and leaf.cxx is not None and leaf.cxx.spelling in UNCROSSABLE:
        raise TypeError(
            f"{where} is a {leaf.cxx.spelling}, and "
            f"{UNCROSSABLE[leaf.cxx.spelling]}")


def _param(p: Param) -> dict[str, Any]:
    """One parameter, as the manifest carries it.

    The default is carried as SOURCE, because every surface above
    writes it into a signature: rendered here from the value the
    declaration's import holds. A vocabulary member is written as the
    member and not as its value, because `'nar'` in a signature says
    nothing about which vocabulary it came from."""
    return {"name": p.name, "type": _type(p.type), "default": _source(p)}


def _source(p: Param) -> str | None:
    """A parameter's default as the Python source that writes it."""
    if not p.has_default:
        return None
    if p.member:
        return f"{p.type.python}.{p.member}"
    return repr(p.default)


def _method(m: Method) -> dict[str, Any]:
    return {
        "name": m.name,
        "params": [_param(p) for p in m.params],
        "return_type": _type(m.ret),
        # Cleaned, unlike the class docstring below. That asymmetry is
        # the current manifest's, not this module's: model.py reads a
        # class doc from __dict__ raw and a method doc through
        # inspect.getdoc, which cleans. Matching it is the point.
        "doc": _clean(m.doc),
    }


def _clean(text: str) -> str:
    import inspect
    return inspect.cleandoc(text) if text else ""


def dunders(decl: Decl) -> list[str]:
    """The value dunders this declaration implies.

    Sorted, like the manifest's, so the two are comparable as lists
    rather than as sets that happen to agree."""
    facts = {
        "value": decl.wire == "value",
        "order": bool(decl.order),
        "text": bool(decl.text),
    }
    return sorted(name for name, fact in DUNDERS if facts[fact])


def _wire_fields(cls: Class) -> list[list[str]]:
    """What this value is made of, in wire spellings.

    One reading of the declaration, shared with the binding: the
    manifest and the emitted `_wire_fields` say the same thing because
    they ask the same question, not because two lists agree.
    `Class.parts` is where that question is answered."""
    return [[f.name, f.type] for f, _ in cls.parts]


def function_entry(fn: Method, package: str, module: str) -> dict[str, Any]:
    """One free function, as the manifest carries it.

    The same shape `model.extract_function` reflected off a live
    Python function, from the declaration instead. A nanobind
    function cannot be reflected at all - it is a builtin, and
    `inspect.signature` refuses one - so for a nanobind module this
    is the only route.

    `threading` is what the function opted INTO, and empty means it
    opted into nothing: `wrapped` is False, so it gets no async form
    and no rpc. It is still surface, so the stubs still describe it -
    leaving it out would hide a real name from a typechecker."""
    policy = fn.policy or None
    if policy is not None:
        for pr in fn.params:
            _crossable(pr.type, f"{fn.name}({pr.name})")
        _crossable(fn.ret, f"{fn.name}'s return")
    return {
        "name": fn.name,
        "module": f"{package}.{module}",
        "threading": policy,
        # No policy means no wrapper: the function is surface, not
        # something the codegen hops a thread for.
        "wrapped": policy is not None,
        "params": [_param(p) for p in fn.params],
        "return_type": _type(fn.ret),
        "doc": _clean(fn.doc),
    }


def words_entry(cls: Class, package: str, module: str) -> dict[str, Any]:
    """One vocabulary entry, in the manifest's own key order.

    Four fields, and each is a member of the declaration read a
    different way: the class name, where the build puts it, the words
    in order, and the class docstring.

    CLEANED, unlike a wrapper's. `model.py` reads a wrapper's
    `__doc__` straight out of `__dict__` and reads an enum's through
    `inspect.getdoc`, which cleans it - so the two routes differ, and
    this follows the route it is being diffed against."""
    return {
        "name": cls.name,
        "module": f"{package}.{module}",
        "values": [m.value for m in cls.members],
        "doc": inspect.cleandoc(cls.doc),
    }


def _ctor_params(cls: Class,
                 functions: Sequence[Method] = ()) -> tuple[Param, ...]:
    """The parameters a caller passes to build one of these.

    Two sources, and only one applies to any class. An ordinary class
    declares `__init__` and that is the signature. A `@produced(by=X)`
    class is built by X, so X's parameters are what a caller passes -
    including X's defaults, which is the part that was being lost."""
    if cls.decl.built_by:
        made = next((f for f in functions if f.name == cls.decl.built_by),
                    None)
        if made is not None:
            return tuple(made.params)
    return tuple(cls.ctor.params) if cls.ctor is not None else ()


def entry(cls: Class, package: str, module: str,
          final: bool = True,
          functions: Sequence[Method] = ()) -> dict[str, Any]:
    """One wrapper entry, in the manifest's own key order.

    Key order matters only for reading a diff, and a diff of this
    against the real manifest is the whole point of the exercise.

    `final` picks WHICH manifest. There are two, a stage apart. The
    one `build_manifest` returns is finished: `message` names the proto
    message and `async_base` names the async twin's base, both filled
    in by a later pass. The one the generator's own extraction hands
    back has neither yet, and `_helpers` beside them - the round-trip
    methods a wire value carries.

    `final=True` is the finished shape, which is what `check.py`
    diffs. `final=False` is the shape the generator consumes, so an
    entry can be handed to it in place of one it reflected. Emitting
    the finished shape into that seam was the first thing tried, and
    it disagreed with the compiled class about two fields nothing had
    filled in yet."""
    decl = cls.decl
    threading = decl.threading
    wire = decl.wire or "proxy"
    if wire == "proxy":
        # A proxy is reached through a service, so its methods ARE
        # messages. A value is not: it crosses whole, as its fields.
        for pr in _ctor_params(cls, functions):
            _crossable(pr.type, f"{cls.name}({pr.name})")
        for m in cls.methods:
            for pr in m.params:
                _crossable(pr.type, f"{cls.name}.{m.name}({pr.name})")
            _crossable(m.ret, f"{cls.name}.{m.name}'s return")
    # The names that follow from the wire kind. A value crosses as a
    # message; a proxy stays where it is and is reached through a
    # service, so it has a service, a way to acquire one, a protocol,
    # and the two generated classes that speak it.
    #
    # Every name is derived from the class's own. The generator builds
    # them the same way, which is what makes a proxy's whole RPC
    # surface knowable from the declaration.
    #
    # Named and typed here rather than spread inline: the two branches
    # carry different value types - one of them holds a None - and a
    # dict literal takes the type of the first branch it sees.
    wire_names: dict[str, Any] = ({
        "service": f"{cls.name}{SERVICE}",
        "acquire": {
            "path": f"/{PROTO_PACKAGE}.{cls.name}{SERVICE}/{ACQUIRE}",
            "req": f"{cls.name}_{ACQUIRE}Req",
        },
        "protocol": f"{cls.name}{PROTOCOL}",
        "async_class": f"{ASYNC}{cls.name}",
        "rpc_class": f"{RPC}{cls.name}",
    } if wire == "proxy" else {
        "message": f"{cls.name}Msg" if final else None,
    })
    return {
        "name": cls.name,
        # The one fact the declaration cannot hold: which package the
        # emitted binding lands in is the build's decision, not the
        # declaration's.
        "module": f"{package}.{module}",
        # RAW, matching model.py: it reads cls.__dict__["__doc__"],
        # which is the literal text, so the stubs carry the same
        # indentation the source had.
        "doc": cls.doc,
        # Empty for a produced value. It binds no C++ type: the object
        # that made it flattened one, so there is no declaration to
        # link a `_binds` name to.
        "binds": "" if cls.is_value else "C" + cls.name,
        # The C++ base, if the declaration named one, spelled the way
        # reflection spelled a Python base: module-qualified, because
        # the stub emitter takes the last component and the async
        # emitter needs to know which module to import it from.
        #
        # One base. Every hierarchy this binds is single inheritance,
        # and C++ multiple inheritance through a Python type is a
        # different problem from the one a declaration is for.
        "bases": ([f"{package}.{module}.{decl.base}"] if decl.base else []),
        "threading": threading,
        # `@abstract`: the C++ FACT. The type has pure virtuals, so a
        # caller holds one most of the time - you ask for a store and
        # use it without caring which implementation answered - and it
        # needs an async wrapper and a wire identity of its own.
        "abstract": decl.abstract,
        # ...and the DERIVED question every layer above actually asks:
        # is there a door. They each read `abstract` and meant this,
        # which is why nix::Store could not state the true fact about
        # itself without losing its factory (tasks/061). Computed once,
        # on the class, so the binding and the wrappers cannot disagree.
        "constructs": cls.constructs,
        # PRODUCED: nothing a caller writes builds one, so a stub
        # says NoReturn for the constructor. `is_value` stood in for
        # this until a produced value bound a real Nix type and
        # stopped being a struct the emitter declares.
        "produced": cls.is_produced,
        # "proxy" is the safe default on both sides: stateful until a
        # declaration proves otherwise.
        "wire": wire,
        "wire_fields": _wire_fields(cls),
        **({"unit": True} if decl.unit else {}),
        # How to walk this type as a TREE, when it is one. A value that
        # holds values cannot be described by wire_fields: the shape is
        # recursive and its arms are the wire kinds themselves. The RPC
        # layer reads this instead of naming the class or its
        # accessors. Absent for everything that is not a tree.
        **({"tree": copy.deepcopy(decl.tree)} if decl.tree else {}),
        "blocking": decl.blocking,
        # Two things a wrapper buys: a hop onto a home thread, and
        # releasing the GIL around a call that waits. A pool class
        # whose methods cannot block needs neither.
        "wrapped": threading == "affine" or decl.blocking,
        "dunders": dunders(decl),
        # The FACTORY's parameters for a produced class, not the
        # `__init__` beside it. A `@produced(by=X)` class is built by
        # X, so X owns the signature - and reading the constructor
        # instead lost `open_store`'s `uri="auto"` everywhere at once:
        # the binding, the stub, the manifest and `AsyncStore`, which
        # required an argument the declaration said was optional.
        #
        # `read.py` refuses parameters on such an `__init__` now, so
        # the two cannot disagree again. This is the other half: the
        # one place they are read from.
        "ctor": [_param(p) for p in _ctor_params(cls, functions)],
        # SURFACE only. A declaration may declare a private method -
        # `Value._identity` is what the RPC tree walk reads to visit a
        # shared value once - and the emitter binds it, because the
        # walk calls it. Nothing generated describes it: a leading
        # underscore is Python's own word for "not surface", and the
        # reflection this replaces dropped one for the same reason.
        "methods": [_method(m) for m in cls.methods
                    if not m.name.startswith("_")],
        # Written by a later stage of the real generator, and a
        # package-level fact rather than a class one.
        "async_base": None,
        # A proxy and a value carry DIFFERENT keys here, not the same
        # keys with different values. A value crosses as a message; a
        # proxy stays where it is and is reached through a service,
        # so it has a service, a way to acquire one, a protocol, and
        # the two generated classes that speak it.
        #
        # Every name is derived from the class's own. The generator
        # builds them the same way, which is what makes a proxy's
        # whole RPC surface knowable from the declaration.
        **wire_names,
        # The round-trip helpers a wire value carries. Derived, not
        # reflected: the emitter writes both for a produced value and
        # both for a constructed one, so a declared value HAS them by
        # construction.
        **({} if final else {
            "_helpers": sorted(("_from_parts", "_parts"))
            if decl.wire == "value" else [],
        }),
    }
