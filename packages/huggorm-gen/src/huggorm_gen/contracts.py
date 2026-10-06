"""The rules a declaration set must obey before anything is emitted.

Each check reads the typed model and returns complaints; the build
prints every one and stops. What the reader already refuses - a union
other than `T | None`, an unknown name - is not checked again here.
"""

from __future__ import annotations

from huggorm_dsl.declare import Crossing, Threading
from huggorm_gen import ir


def wrap(model: ir.Model) -> list[str]:
    """An unwrapped class must be self-contained.

    Callers touch an unwrapped class's sync binding object directly.
    So it may not be affine - there is no thread to hop to - and it may
    not hand back an object that IS wrapped: the caller would get a
    bare sync instance with no runner and no await to get one."""
    bad = []
    for c in model.classes.values():
        if c.wrapped:
            continue
        if c.threading is not Threading.POOL:
            bad.append(f"{c.name}: an unwrapped class must be threading "
                       f"'pool', not '{c.threading}'")
        for m in c.methods:
            if (a := model.adopted(m.returns)) is not None and a.wrapped:
                bad.append(
                    f"{c.name}.{m.name} returns {m.return_spelling}, which "
                    f"needs a wrapper. An unwrapped class cannot attach "
                    f"one; declare _blocking on one side or the other so "
                    f"the two agree.")
    return bad


def collection(model: ir.Model) -> list[str]:
    """No method may return a CONTAINER of wrapped types.

    Every layer attaches a runner to one object: the async wrapper
    adopts one, the server puts one handle, the client builds one
    proxy. None walks a container, so `dict[str, Value]` builds and
    then hands back bare sync objects. A collection of remote objects
    is a value TREE, which is protocol (huggorm#30)."""
    wrapped = {n for n, c in model.classes.items() if c.wrapped}
    return [
        f"{c.name}.{m.name} returns {m.return_spelling}, a collection "
        f"holding {m.returns.name}. Nothing attaches a runner to the "
        f"elements of a container. Return the container's owner and let "
        f"the caller walk it, or realize it as a value tree (huggorm#30)."
        for c in model.classes.values() for m in c.methods
        if m.returns is not None and m.returns.name in wrapped
        and m.returns.required.origin
    ]


def affine_from_pool(model: ir.Model) -> list[str]:
    """No POOL class may return an AFFINE one.

    An affine object lives on the thread that made it, and a pool
    object's methods run on any pool thread, so it would be born on a
    thread nothing owns. Every pool class is checked, so a chain is
    covered too: any path from pool to affine has one such edge
    (huggorm#8)."""
    return [
        f"{c.name}.{m.name} returns {m.return_spelling}, which is affine, "
        f"from a pool class: it would live on a thread nothing owns. "
        f"Return it from an affine class instead."
        for c in model.classes.values() if c.threading is Threading.POOL
        for m in c.methods
        if (a := model.adopted(m.returns)) is not None
        and a.wrapped and a.threading is Threading.AFFINE
    ]


def free_functions(model: ir.Model) -> list[str]:
    """A free function's served return must be adoptable from the pool.

    Its coroutine adopts the return as a method's is, so it must be one
    object, `X` or `X | None`. And only a POOL class: an affine one
    needs a home thread, and a free function has none. Otherwise the
    coroutine hands a sync object to an async caller."""
    bad = []
    for fn in (*(f for f in model.functions.values() if f.wrapped),
               *model.blocking_methods):
        r = fn.returns
        if r is None or r.leaf.kind != "proxy":
            continue
        adopted = model.adopted(r)
        if adopted is None:
            bad.append(
                f"free function {fn.name} returns {r.spelling}, and "
                f"{r.leaf.name} cannot be adopted into an async form "
                f"from a free function. Return a served class on its "
                f"own or as `X | None`.")
        elif adopted.threading is not Threading.POOL:
            bad.append(
                f"free function {fn.name} returns {adopted.name}, which is "
                f"{adopted.threading}: it needs a home thread and a free "
                f"function has none. Return it from a method of the class "
                f"that owns the thread instead.")
    return bad


def wire(model: ir.Model) -> list[str]:
    """The wire policy and the serialization contract must agree.

    A value promises it can be rebuilt from its parts; a proxy promises
    it cannot and stays behind a handle. A value with no fields used to
    surface as a KeyError deep in the server at the first call."""
    bad = []
    for c in model.classes.values():
        if c.wire is Crossing.PROXY:
            if c.wire_fields:
                bad.append(f"{c.name}: proxy types travel as handles, drop "
                           f"_wire_fields")
            continue
        if not c.wire_fields and not c.semantics.unit:
            bad.append(f"{c.name}: wire-value needs _wire_fields describing "
                       f"its message, or @wire_value(unit=True) if it has "
                       f"none")
        if c.wire_fields and c.semantics.unit:
            bad.append(f"{c.name}: a unit value has no parts, drop unit=True")
        if c.threading is not Threading.POOL:
            # A value that may not leave its thread cannot be
            # serialised off it.
            bad.append(f"{c.name}: wire-value must be threading 'pool', "
                       f"not '{c.threading}'")
        for f in c.wire_fields:
            bad += _field(model, c.name, f)
    return bad


def _field(model: ir.Model, owner: str, f: ir.FieldModel) -> list[str]:
    where = f"{owner}._wire_fields {f.name!r}"
    if (why := ir.wire_blocker(f.type, model.served)) is not None:
        return [f"{where}: {why}"]
    if f.type.leaf.kind == "proxy":
        # A wire-value is rebuilt on the far side by _from_parts, which
        # needs a local object for every part, and a proxy has none
        # there (huggorm#31).
        return [f"{where}: {f.type.leaf.name} is a proxy, so _from_parts has "
                f"nothing to rebuild it from on the far side. A wire-value "
                f"copies all the way down. Carry the proxy as a method "
                f"parameter or return instead."]
    return []


def complaints(model: ir.Model) -> list[tuple[str, str]]:
    """Every broken rule, as (rule, complaint)."""
    return [(rule, why)
            for rule, check in (("wrap contract", wrap),
                                ("collection contract", collection),
                                ("policy", affine_from_pool),
                                ("free function contract", free_functions),
                                ("wire contract", wire))
            for why in check(model)]
