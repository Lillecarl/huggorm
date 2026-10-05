"""The `check_*` contracts the build refuses to pass, over the
manifest dicts. They move onto `ir.Model` with the rest (huggorm#29).
"""

from typing import Any

from huggorm_gen.payload.wiretypes import (
    adoptee,
    list_value,
    map_value,
    names_in,
    optional_value,
    scalar_spelling,
)

# One class or function, reflected into the plain dict every layer
# above reads. Named rather than spelled dict[str, Any] everywhere:
# it is the contract between the sources and the emitter, and the one
# place to tighten if it becomes a TypedDict (huggorm#29).
Proto = dict[str, Any]

# The package the bindings live in. A type declared there is spelled
# bare in a signature, because the module that names it is the one
# that defines it.
BINDINGS_PKG = "huggorm_bindings"

# The dunders a value type may define, and the three it must. Ordering
# and __str__ are per-class: a store path has a natural order and a
# natural string, a PathInfo has neither.
VALUE_DUNDERS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__",
                 "__hash__", "__repr__", "__str__")
REQUIRED_DUNDERS = ("__eq__", "__hash__", "__repr__")

def check_wire_contract(protos: list[Proto],
                        enums: set[str] | None = None,
                        unions: set[str] | None = None,
                        errors: set[str] | None = None) -> list[str]:
    """The wire policy and the serialization contract must agree.

    A "value" type promises the RPC layer it can be rebuilt from its
    parts; a "proxy" promises it cannot and must stay behind a handle.
    A value with no _wire_fields, or missing round-trip helpers, used to
    surface as a KeyError deep inside the server on the first call that
    touched it. Fail the build instead, naming the type.

    Returns a list of complaints; empty means the contract holds."""
    # A string enum is a NAME the wire knows and not a class in the
    # manifest's groups, so it has to be handed in. Without it a
    # _wire_fields entry of enum type failed as "unknown field type" -
    # refused here while the rpc layer accepted the same declaration
    # anywhere a scalar goes (huggorm#47).
    # A UNION is neither a class in the groups nor an enum, and it is
    # a legal field type: `DerivedPathBuilt.drv_path` is a
    # SingleDerivedPath, which is an alias over two arms. The arms
    # themselves are checked where they are declared - the reader
    # refuses a scalar, a vocabulary or a proxy arm - so by the time a
    # name reaches here, being a union is enough.
    # An ERROR class is a legal field type too, and the last of the
    # three that is not a class in the groups. A KeyedBuildResult's
    # failure arm IS a declared exception (huggorm#71), and one
    # already has a message of its own - the fault detail every typed
    # error crosses in - so the field points at that.
    known = ({p["name"] for p in protos} | (enums or set())
             | (unions or set()) | (errors or set()))
    kinds = {p["name"]: p["wire"] for p in protos}
    bad = []
    for proto in protos:
        name, fields = proto["name"], proto["wire_fields"]
        if proto["wire"] == "proxy":
            if fields:
                bad.append(f"{name}: proxy types travel as handles, drop _wire_fields")
            continue
        if proto["wire"] != "value":
            bad.append(f"{name}: unknown _wire {proto['wire']!r} (value|proxy)")
            continue
        if not fields and not proto.get("unit"):
            bad.append(f"{name}: wire-value needs _wire_fields describing its "
                       f"message, or @wire_value(unit=True) if it has none")
        if fields and proto.get("unit"):
            bad.append(f"{name}: a unit value has no parts, drop unit=True")
        if proto["threading"] != "pool":
            # A value that may not leave its thread cannot be serialised
            # off it; the two policies contradict each other.
            bad.append(
                f"{name}: wire-value must be threading 'pool', not "
                f"{proto['threading']!r}")
        for fname, ftype in fields:
            optional = ftype.endswith("?")
            ftype = ftype.removesuffix("?")
            # A container field is a repeated protobuf field, so what
            # goes under test is the type it HOLDS. The declaration is
            # the same one an annotation uses - `list[StorePath]` - and
            # it is read by the same code, so the two cannot drift.
            try:
                element = list_value(ftype) or map_value(ftype)
            except TypeError as e:
                bad.append(f"{name}._wire_fields {fname!r}: {e}")
                continue
            if element is not None:
                if optional:
                    # A repeated field has no presence, so an absent
                    # one and an empty one are the same field. "?"
                    # would promise a distinction that cannot exist.
                    bad.append(
                        f"{name}._wire_fields {fname!r}: a container cannot "
                        f"be optional. A repeated field has no presence, so "
                        f"an absent one IS an empty one - drop the '?'.")
                ftype = element
            # Against what the WIRE carries, the list the schema and
            # the codec both read: a field cannot be `float` or `None`
            # and can be `bytes`. A SPELLED scalar answers too - a
            # `datetime.timedelta` goes in an int field.
            if scalar_spelling(ftype) is None and ftype not in known:
                bad.append(f"{name}._wire_fields {fname!r}: unknown field type {ftype!r}")
            elif kinds.get(ftype) == "proxy":
                # The schema would carry it: _msg_arg_type turns a proxy
                # into a Handle field wherever it appears. The codec
                # cannot, and not by omission. A wire-value is rebuilt
                # on the far side by _from_parts, which needs a real
                # local object for every part - and a proxy is exactly
                # the thing that has no object on the far side. Nesting
                # one here produces a type that only its own process can
                # reconstruct. Say so at build time rather than at the
                # first call that touches the field (huggorm#31).
                bad.append(
                    f"{name}._wire_fields {fname!r}: {ftype} is a proxy, so "
                    f"_from_parts has nothing to rebuild it from on the far "
                    f"side. A wire-value copies all the way down. Carry the "
                    f"proxy as a method parameter or return instead.")
        for helper in ("_parts", "_from_parts"):
            if helper not in proto["_helpers"]:
                bad.append(f"{name}: wire-value needs a {helper} round-trip helper")
        for dunder in REQUIRED_DUNDERS:
            if dunder not in proto["dunders"]:
                # A value that does not compare is a value in name
                # only: two of them naming the same thing are unequal,
                # a set of them deduplicates nothing, and repr() shows
                # an address. All three answers are derivable from the
                # _wire_fields this class already declares - see
                # _value.py - so there is nothing to weigh up.
                bad.append(
                    f"{name}: wire-value needs {dunder}. A value compares, "
                    f"hashes and prints as what it is, and _value.py "
                    f"derives all three from _wire_fields.")
    return bad


# Three functions stood here and all three reflected: `extract_enum`
# read a live StrEnum's members, `extract_errors` imported the emitted
# exception module and walked it, and `check_error_contract` built one
# of each error to prove `cls(*parts)` round-trips.
#
# The first two are derived now - `cppgen.declared_enums` and
# `cppgen.declared_errors` answer from the declaration, and both were
# checked against these before they went.
#
# The third could not be derived and was not meant to be: it runs
# code, which is the one thing a declaration cannot do for you. It is
# a test now, in `huggorm/tests/test_contracts.py`, where importing
# the compiled package is honest. The build sandbox runs that suite,
# so it is still a build gate.


def check_wrap_contract(protos: list[Proto]) -> list[str]:
    """An unwrapped class must be self-contained.

    Not wrapping a class means callers touch the sync binding object
    directly. Two things then have to hold. It may not be affine -
    there would be no thread to hop to - which the wrapped rule already
    guarantees. And it may not hand back an object that IS wrapped: the
    caller would receive a bare sync instance of a type that needs a
    runner, with no runner attached and no await to get one.

    Returns a list of complaints; empty means the contract holds."""
    wrapped = {p["name"] for p in protos if p["wrapped"]}
    bad = []
    for proto in protos:
        if proto["wrapped"]:
            continue
        if proto["threading"] != "pool":
            bad.append(
                f"{proto['name']}: an unwrapped class must be threading "
                f"'pool', not {proto['threading']!r}")
        for m in proto["methods"]:
            if adoptee(m["return_type"], wrapped) is not None:
                bad.append(
                    f"{proto['name']}.{m['name']} returns {m['return_type']}, "
                    f"which needs a wrapper. An unwrapped class cannot "
                    f"attach one; declare _blocking on one side or the "
                    f"other so the two agree.")
    return bad


def check_optional_contract(protos: list[Proto]) -> list[str]:
    """An optional return names one type and None.

    `T | None` works because a protobuf message field has presence, so
    the wire needs nothing new. A wrapped T is adopted when it is there
    and None passes through: the async wrapper writes no wrapper, the
    server fills no handle, and the client reads an unset field as
    None (`wiretypes.adoptee`).

    Returns a list of complaints; empty means the contract holds."""
    bad = []
    for proto in protos:
        for m in proto["methods"]:
            try:
                optional_value(m["return_type"])
            except TypeError as e:
                bad.append(f"{proto['name']}.{m['name']}: {e}")
    return bad


def affine_from_pool(protos: list[Proto]) -> list[str]:
    """No POOL class may return an AFFINE one.

    An affine object lives on the thread that made it, and a pool
    object's methods run on any pool thread, so it would be born on a
    thread nothing owns. `attach_runner` refuses it when the call runs;
    this refuses the declaration first.

    Every pool class is checked, a returned type as much as a wrapper,
    so a chain is covered too: any path from a pool class to an affine
    one ends in one such edge (huggorm#8).

    Returns a list of complaints; empty means the rule holds."""
    affine = {p["name"] for p in protos
              if p["wrapped"] and p["threading"] == "affine"}
    return [
        f"{proto['name']}.{m['name']} returns {m['return_type']}, which is "
        f"affine, from a pool class: it would live on a thread nothing "
        f"owns. Return it from an affine class instead."
        for proto in protos if proto["threading"] == "pool"
        for m in proto["methods"]
        if adoptee(m["return_type"], affine) is not None
    ]


def check_collection_contract(protos: list[Proto]) -> list[str]:
    """No method may return a COLLECTION of wrapped types.

    A wrapped type only works when something attaches a runner to it,
    and every layer does that for one object: the async wrapper writes
    `AsyncX._adopt(result, self._runner)`, the server puts one handle, the
    client builds one proxy. None of them walks a container, so
    `dict[str, Value]` type-checks, builds, emits a schema - and hands
    back bare sync objects in process while failing on the first call
    over the wire.

    That is the same accepted-then-explodes shape a proxy in
    _wire_fields had. It is not an oversight either: a collection of
    remote objects is a value TREE, which is what Realize answers, and
    that is protocol rather than something a return annotation can ask
    for (huggorm#30).

    Returns a list of complaints; empty means the contract holds."""
    wrapped = {p["name"] for p in protos if p["wrapped"]}
    bad = []
    for proto in protos:
        for m in proto["methods"]:
            rt = m["return_type"]
            if adoptee(rt, wrapped) is not None:
                continue  # one wrapped object, or None, is the supported case
            for named in sorted(names_in(rt) & wrapped):
                bad.append(
                    f"{proto['name']}.{m['name']} returns {rt}, a collection "
                    f"holding {named}. Nothing attaches a runner to the "
                    f"elements of a container. Return the container's owner "
                    f"and let the caller walk it, or realize it as a value "
                    f"tree (huggorm#30).")
    return bad
