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
# `datetime.timedelta` is the only one, and it is what a DURATION is
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
# Not a protobuf well-known `Duration`. That message is seconds plus
# nanos, which is a second representation to convert through and a
# precision neither end has.
SPELLED = {"datetime.timedelta": "int"}

def python_spelling(type_str: str) -> str:
    """The Python type a wire scalar is above the boundary.

    `uint` is an `int` there: the width is a fact about the crossing.
    Every other name answers itself, a class's included."""
    return "int" if type_str == "uint" else type_str


# Every Nix attribute name is a string, so a map key is always one.
# That is what makes an attribute set representable as a protobuf map
# at all (huggorm#30).
MAP_KEY = "str"

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


def arm_field(arm: str) -> str:
    """One union arm's field name inside its oneof.

    The arm's own type name, lowercased with underscores, so
    `DerivedPathBuilt` is `derived_path_built` and a reader of the
    schema sees which arm they have without a table.

    Here rather than in either side, because BOTH sides name it: the
    schema builder when it writes the field, and the codec when it
    reads `WhichOneof` back."""
    out: list[str] = []
    for i, ch in enumerate(arm):
        if ch.isupper() and i:
            out.append("_")
        out.append(ch.lower())
    return "".join(out)


def entry_name(field_name: str) -> str:
    """The synthesised MapEntry message for one map field, following
    protobuf's own convention: field `gc_counts` -> `GcCountsEntry`."""
    return "".join(part.title() for part in field_name.split("_")) + "Entry"
