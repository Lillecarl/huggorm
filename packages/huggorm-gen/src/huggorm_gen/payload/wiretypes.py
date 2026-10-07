"""
The wire's own facts about a type, shared by the build and the codec.

Which builtins go in a field as themselves, which declared type goes
in one as a builtin, how a union arm and a map entry are named, and
how deep a union may nest. The build resolves every type into a
`Wire` descriptor; these are the few facts both sides still state.

It is copied into the generated package as `_wiretypes.py`, the same
way `_runtime.py` is, so the two sides read one definition instead of
two that drift.
"""

import datetime
import os
import pathlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# The types that go in a field as themselves. The schema maps them to
# proto types and the codec maps them to constructors; both start here.
#
# bytes is here because a store holds FILES. Their contents are not
# text and must not be encoded as if they were: a str field would
# round-trip a NAR into mojibake, and the hash that names the store
# path would be a hash of the wrong thing.
#
# `uint` is here because C++ has two 64-bit integers and the wire
# needs both. `int` is a sint64, which holds an int64_t and half of a
# uint64_t; `uint` is a uint64. Above the boundary both are a Python
# `int` - the width is a fact about the CROSSING, not about the type a
# caller sees.
#
# Found by `GCOptions.max_freed`, whose upstream default is the
# largest uint64_t: sending the default options object raised
# `ValueError: Value out of range: 18446744073709551615` (huggorm#79).
SCALAR_NAMES = ("str", "int", "uint", "float", "bool", "bytes")

# A declared type that is not a builtin and still goes in a field as
# one, with the builtin it goes in as.
#
# `datetime.timedelta` is what a DURATION is
# above every boundary: nanobind's own chrono caster hands a
# `std::chrono::microseconds` over as one, so the in-process surface
# needs nothing of ours (huggorm#71).
#
# It crosses as an int of MICROSECONDS. Carl's decision, and the two
# reasons agree: a timedelta's own finest unit IS the microsecond, so
# nothing is rounded, and upstream holds the same resolution - a
# coarser wire would lose a build's CPU time on the way through and
# the round-trip gate would say so.
#
# Not seconds plus nanos: that is a second representation to convert
# through, and a precision neither end has.
_MICROSECOND = datetime.timedelta(microseconds=1)


def _duration_out(value: datetime.timedelta) -> int:
    """A duration as the whole microseconds a field carries.

    Floor division by one microsecond, which is exact: a timedelta
    holds days, seconds and microseconds as integers, so there is no
    remainder to lose."""
    return value // _MICROSECOND


def _duration_in(raw: int) -> datetime.timedelta:
    """The microseconds a field carried, as a duration again."""
    return datetime.timedelta(microseconds=raw)


@dataclass(frozen=True, slots=True)
class Spelled:
    """How one declared type goes in a builtin field.

    `field` is the builtin, which the schema reads. `out` and `back`
    convert at each end, which the codec reads. Two converters, unlike
    a vocabulary: a StrEnum member IS a str, so its one constructor
    serves both directions, and a timedelta is not an int."""

    field: str
    out: Callable[[Any], Any]
    back: Callable[[Any], Any]


# A filesystem path crosses as the str it is. It names a file on the
# SERVER's machine, which #142 makes the client's: a same-uid Unix
# socket. A socket forwarded over ssh or into a container breaks that,
# and the path then names a file the client cannot open (huggorm#148).
SPELLED = {
    "datetime.timedelta": Spelled("int", _duration_out, _duration_in),
    "pathlib.Path": Spelled("str", os.fspath, pathlib.Path),
}

# The scalars a value tree's leaf may be.
TREE_LEAVES = frozenset({"str", "int", "bool", "float"})

def python_spelling(type_str: str) -> str:
    """The Python type a wire scalar is above the boundary.

    `uint` is an `int` there: the width is a fact about the crossing.
    Every other name answers itself, a class's included."""
    return "int" if type_str == "uint" else type_str


# How deep a UNION may nest before the codec refuses.
#
# A union arm may hold the union again - that is what lets a
# SingleDerivedPath name the output of a derivation that is itself an
# output - so a peer can send a chain of any length. The wire is a
# trust boundary, and without a cap the answer is a RecursionError
# that reaches a caller as an anonymous InternalError.
#
# Generous on purpose: anything real is one or two deep, so only a bug
# or an attack sees this.
MAX_UNION_DEPTH = 32
