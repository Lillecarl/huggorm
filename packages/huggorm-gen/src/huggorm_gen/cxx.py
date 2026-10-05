"""Declared types -> C++ spellings.

How a declared type is spelled in a nanobind signature, by value and
as a parameter, and the caster each spelling needs. One answer for
every stage that writes C++.
"""

import json
from collections.abc import Mapping
from typing import Protocol

from huggorm_dsl.declare import Decl
from huggorm_dsl.read import Class, Param, Type

# How a declared type is spelled in a C++ signature, and which caster
# has to be included for it to cross. Nothing here is guessed from a
# Python name: a type reaches this table only through an Annotated
# alias that already carries its C++ spelling.
CXX_PARAM = {
    "string": ("const std::string &", "string"),
    "string_view": ("std::string_view", "string_view"),
    "bint": ("bool", None),
    "uint64_t": ("std::uint64_t", None),
    "int64_t": ("std::int64_t", None),
    "double": ("double", None),
    # A duration, which nanobind casts to a datetime.timedelta both
    # ways - so the caster IS the whole binding and nothing here
    # converts. `chrono` names <nanobind/stl/chrono.h>, like every
    # other entry names its own header.
    "microseconds": ("std::chrono::microseconds", "chrono"),
    # A Python callable the binding KEEPS. No caster header: nanobind
    # itself defines `nb::object`, and there is nothing to convert -
    # the point is to hold the reference, not to read a value out.
    #
    # BY VALUE, which is the opposite of every other entry, and the
    # method carrying it must be `@instant`. A by-value Python handle
    # is only safe while the GIL is HELD, and `@instant` is what keeps
    # it: a method that released it would change a reference count
    # without it and nanobind aborts the process. Registration waits
    # for nothing, so it has no reason to release the GIL anyway.
    "nb::object": ("nb::object", None),
}

# A declared Python type with no C++ alias behind it, and the C++ it
# crosses as. Each entry is a caster nanobind ships, so nothing here
# is flattened on the way through.
#
# This is what decided the backend. The Cython route could declare
# none of these - a pxd has no std::set, no std::optional and no
# non-default-constructible member - so it turned every one of them
# into a string and parsed it back. nanobind casts them, so a store
# path stays a store path from libstore to Python.
CXX_PYTHON = {
    "bytes": ("nb::bytes", None),
    "pathlib.Path": ("const std::filesystem::path &", "filesystem"),
}

# The plain reading of a builtin, for a declaration that annotates
# with the builtin and no alias. A LAST resort, unlike CXX_PYTHON
# above: an alias is the declaration saying which C++ it means, and
# `str` alone says only that Python sees a str.
#
# The difference is what `StrView` and `Path` need. Both are `str`-ish
# to Python and both carry Cxx("string"), which is the coarsest of
# the three answers - so a table that let either side win outright
# would turn one of them into the wrong crossing.
CXX_BUILTIN = {
    "str": ("const std::string &", "string"),
    "bool": ("bool", None),
}


# Where an emitted C++ type goes. The same namespace `errors.hpp`
# already opens, so a translation unit that includes it reopens one
# namespace rather than gaining a second.
NAMESPACE = "huggorm"


class Declared(Protocol):
    """A declared class, read or resolved: both carry these two."""

    @property
    def name(self) -> str: ...

    @property
    def decl(self) -> Decl: ...


def held(cls: Declared) -> str:
    """The C++ type this class binds.

    Two sources, and which one applies is the difference between a
    HANDLE and a RECORD. `@binding(cxx=...)` names a type libstore
    already has, and the binding holds one of those. A produced value
    names none, because libstore has no type shaped like the answer -
    `queryPathInfo` hands back a ValidPathInfo whose fields a caller
    reads one at a time. So the emitter declares the struct, and the
    name is the class's own."""
    return cls.decl.cxx or f"{NAMESPACE}::{cls.name}"


def bare(cls: Class, known: Mapping[str, Class]) -> str:
    """The C++ type behind a declared class, or a refusal."""
    if cls.is_union:
        # A SUM, and std::variant is what C++ already calls one.
        # nanobind casts it natively (<nanobind/stl/variant.h>), so a
        # parameter of a union type needs no dispatch written by hand:
        # the caster tries each arm and the body receives the one that
        # matched.
        #
        # The ARMS, not the C++ union type upstream declares.
        # nix::DerivedPath IS a std::variant, but over
        # DerivedPathOpaque rather than StorePath - and nanobind's
        # caster is specialised on std::variant exactly, not on
        # something deriving from one. So the binding takes the arms
        # the PYTHON side has and a body converts, which is a decision
        # and belongs in a body.
        if cls.decl.variant is not None:
            # THE UNION'S OWN TYPE. A generated type_caster casts it
            # to the arms Python has, so a signature names what
            # libstore names and no body converts.
            return cls.decl.variant.cxx
        return arms_type(cls, known)
    if cls.is_words:
        # A vocabulary. The member IS the string a Nix parser takes,
        # so it crosses as one - a fact about the words rather than
        # about either binding.
        return "std::string"
    if not cls.decl.cxx and not cls.decl.produced:
        raise TypeError(
            f"'{cls.name}' has no C++ type behind it. Only a class with "
            f"@binding(cxx=...) or @produced can cross as one.")
    return held(cls)


def arms_type(cls: Class, known: Mapping[str, Class]) -> str:
    """A union as the std::variant of the arms PYTHON has.

    Not what a signature says any more - that is the union's own C++
    type - but what the caster casts through, and what `from_arms`
    takes."""
    inner = ", ".join(arm(cls, a, known) for a in cls.decl.arms)
    return f"std::variant<{inner}>"


def arm(cls: Class, name: str, known: Mapping[str, Class]) -> str:
    """One arm as Python has it, in C++: a builtin from the alias the
    declaration wrote, and a class through its own declaration."""
    if (scalar := cls.decl.scalars.get(name)) is not None:
        return value(scalar, known)[0]
    return bare(known[name], known)


def value(t: Type, known: Mapping[str, Class]) -> tuple[str, str | None]:
    """A declared type as C++ carries it BY VALUE, and its caster.

    The value form, not the parameter form. A return is a value, a
    vector's element is a value, and an optional's payload is a
    value - so this is the shape everything else is built from and
    `param` adds the reference where a parameter wants one."""
    held_type = t.required
    other = None if held_type.origin else known.get(held_type.python)
    if other is not None and other.decl.kind == "error":
        # A live Python EXCEPTION, handed over rather than raised. A
        # BuildResult's failure arm is a nix::BuildError, and reading
        # a failed result is not an exception (huggorm#71) - so what
        # crosses is the object.
        #
        # `nb::object` whether or not the declaration wrote `| None`,
        # and the optional is dropped on purpose: `nb::none()` IS the
        # absent value here, and `std::optional<nb::object>` would
        # give a Python caller one spelling for absent and the emitter
        # two.
        return "nb::object", None
    if t.optional:
        held, _ = value(t.required, known)
        return f"std::optional<{held}>", "optional"
    if t.origin == "list":
        held, _ = value(t.element, known)
        # A vector, not the std::set libstore keeps them in. A set
        # casts to a Python set, which has no order - and every one
        # of these answers is sorted, which is information a caller
        # can use.
        return f"std::vector<{held}>", "vector"
    if t.origin == "dict":
        held, _ = value(t.element, known)
        # `std::map`, which is what libstore keeps every one of these
        # in - `OutputPathMap` and `SingleDrvOutputs` are both one -
        # and what nanobind's <nanobind/stl/map.h> casts.
        #
        # str keys only, and that is the wire rather than a shortcut:
        # a protobuf map key is an integral or a string, so a map
        # keyed by anything else has no field to be. The declaration
        # spells `dict[str, V]` and nothing else parses.
        #
        # This replaced a hard-coded `"dict[str, int]": nb::dict`
        # entry that served one free function. A body that builds an
        # nb::dict by hand IS the mapping this exists to derive, so
        # the entry went and `gc_stats` returns the map.
        return f"std::map<std::string, {held}>", "map"
    inner = t.python
    if t.bound or inner in known:
        if inner not in known:
            raise TypeError(
                f"'{inner}' names a class this run has not read. Pass its "
                f"declaration too, so the C++ spelling can be resolved.")
        other = known[inner]
        spelled = bare(other, known)
        if other.decl.holder:
            # Held through something. `@binding(holder="shared_ptr")`
            # is the declaration saying the factory hands back a
            # reference-counted handle, so Python has to keep a share
            # or the object closes under the name for it.
            return (f"std::{other.decl.holder}<{spelled}>",
                    other.decl.holder)
        if other.is_union:
            # <nanobind/stl/variant.h>, and the ARMS' casters too: a
            # variant of bound classes needs none of its own, but one
            # holding a string or a vector does.
            return spelled, "variant"
        return spelled, "string" if spelled == "std::string" else None
    # Three tables, in the order the declaration meant them. A Python
    # type nanobind casts natively wins outright - `pathlib.Path`
    # carries Cxx("string"), the coarse answer, and nanobind has a
    # filesystem caster. Then the alias, which is the declaration
    # naming a C++ spelling. Then the bare builtin, which names none.
    spelled, caster = "", None
    if inner in CXX_PYTHON:
        spelled, caster = CXX_PYTHON[inner]
    elif t.cxx is not None and t.cxx.spelling in CXX_PARAM:
        spelled, caster = CXX_PARAM[t.cxx.spelling]
    elif inner in CXX_BUILTIN:
        spelled, caster = CXX_BUILTIN[inner]
    else:
        raise TypeError(
            f"'{t.python}' has no C++ spelling. A bound class names types "
            f"through an Annotated alias in declare.py.")
    # Both tables spell a PARAMETER, so both may carry a reference.
    # A value never does: a `const std::string &` member of a struct
    # is a dangling reference waiting to happen, and an optional
    # cannot hold one at all.
    return spelled.removeprefix("const ").removesuffix(" &"), caster


def param(t: Type, known: Mapping[str, Class]) -> tuple[str, str | None]:
    """The C++ spelling of a declared type, and the caster it needs.

    A BOUND type - one naming another declared class - resolves
    through `known`, which maps a declared name to its C++ spelling.
    So `is_valid_path(path: StorePath)` becomes `const nix::StorePath
    &`, and neither declaration repeats the other's C++ name."""
    if t.optional or t.origin or t.python in CXX_PYTHON:
        spelled, caster = value(t, known)
        # By const reference, because these are the types worth not
        # copying - and, for `nb::bytes`, because a copy would be
        # WRONG. A method that releases the GIL runs its whole body
        # with the guard held, so a by-value Python handle changes a
        # reference count without the GIL and nanobind aborts the
        # process: "attempted to change the reference count of a
        # Python object while the GIL was not held". A reference
        # binds to the caster's own object, which nanobind destroys
        # after the guard.
        if spelled.startswith("const "):
            return spelled, caster
        return f"const {spelled} &", caster
    if t.bound:
        if t.python not in known:
            raise TypeError(
                f"'{t.python}' names a class this run has not read. Pass its "
                f"declaration too, so the C++ spelling can be resolved.")
        other = known[t.python]
        if other.is_words:
            # A vocabulary. The member IS the string a Nix parser
            # takes, so it crosses as one. That is a fact about the
            # words rather than about the binding, which is why the
            # emitted module is plain Python with no C++ at all.
            return CXX_PARAM["string"]
        # A wire value is a copy the call reads, so const. A proxy is an
        # object the call may act on: `nix::copyClosure` writes into the
        # destination `Store &`, and a const reference cannot reach it.
        if other.decl.wire:
            return f"const {bare(other, known)} &", None
        return f"{bare(other, known)} &", None
    if t.cxx is None or t.cxx.spelling not in CXX_PARAM:
        raise TypeError(
            f"'{t.python}' has no C++ parameter spelling. A bound class "
            f"names types through an Annotated alias in declare.py.")
    return CXX_PARAM[t.cxx.spelling]


def absent(pr: Param) -> bool:
    """Whether this parameter's absence is spelled `None`.

    A CONTAINER whose declared default is None. The declaration's own
    docstring says what that means - a repeated field has no presence
    and needs none, so an absent container IS an empty one - and a
    caller passing None explicitly means the same thing.

    It matters because nanobind's vector caster refuses None: it asks
    for a sequence, and None is not one. So a parameter that reads
    None has to say so in its own type."""
    if not pr.has_default or pr.default is not None:
        return False
    return pr.type.required.origin == "list"


def default(pr: Param) -> str:
    """A Python default, as C++ spells the same value.

    Two cases the table cannot hold, because both need the
    declaration to resolve them.

    A VOCABULARY member is a name in Python and a string in C++:
    `HashAlgorithm.SHA256` is `"sha256"`, and only the vocabulary
    knows which. Emitting the Python spelling put an undeclared
    identifier in the C++.

    `None` on a CONTAINER is an empty one. A repeated field has no
    presence and needs none - an absent container IS an empty one,
    which is what the declaration's own docstring says - so `nullptr`
    would be a null reference where a value belongs."""
    if not pr.has_default:
        return ""
    value = pr.default
    if pr.member:
        # A vocabulary member IS the string a Nix parser takes.
        return json.dumps(value)
    if value is None and pr.type.optional:
        # An optional parameter, absent. `nullptr`, what a bare None
        # becomes below, is a null POINTER, which a std::optional
        # parameter cannot take.
        return "nb::none()"
    if absent(pr):
        # None, and the signature says so. The parameter arrives as a
        # std::optional and an emitted line turns it into an empty
        # container - so a caller who passes nothing and a caller who
        # passes None get the same answer.
        return "nb::none()"
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "nullptr"
    if isinstance(value, str):
        # Double quotes: `'auto'` in C++ is a character literal.
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    raise TypeError(f"{pr.name}: no C++ spelling for the default {value!r}")


def parsed_by(t: Type | None, known: Mapping[str, Class]) -> str:
    """The C++ that turns this vocabulary's string into its type.

    `@words(parsed_by=...)` names it, and it was prose until this read
    it: both `add_*` methods hand-wrote
    `nix::ContentAddressMethod::parse(method)` in their bodies while
    the declaration two files away already said what the parser is
    called. Empty for anything that is not a vocabulary, and for a
    vocabulary that declares no parser - which crosses as its string
    and is parsed by whatever it is handed to."""
    if t is None:
        return ""
    held = t.required
    other = None if held.origin else known.get(held.python)
    if other is None or not other.is_words:
        return ""
    if not other.decl.parsed_by and other.decl.enumerated:
        # Upstream has no parser for this enum, so the emitter wrote
        # one. `from_word` is a template because a return type does
        # not overload - `as_word` going the other way needs no such
        # thing, which is why the two names are not symmetrical.
        return (f"{NAMESPACE}::from_word"
                f"<{other.decl.enumerated.held}>")
    return other.decl.parsed_by


def collection(t: Type | None, known: Mapping[str, Class]) -> str:
    """The C++ collection type for a declared `list[T]`, if T names one.

    Empty for everything else, which is every list whose element type
    is a primitive or whose class is happy with a vector."""
    if t is None or t.origin != "list":
        return ""
    element = known.get(t.element.python)
    return element.decl.collection if element else ""


def handle(t: Type | None, known: Mapping[str, Class]) -> Class | None:
    """The declared class behind this type, when it binds a HANDLE.

    A handle is a class whose `@binding` carries `via`: the bound C++
    type owns a lifetime and the object worth calling is one step
    further in. `None` for everything else, which is almost every
    type - a bound class that binds its own methods is not a handle,
    and neither is a str."""
    if t is None or not t.required.bound:
        return None
    other = known.get(t.required.python)
    return other if other is not None and other.decl.via else None
