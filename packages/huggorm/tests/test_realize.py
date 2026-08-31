"""
Collections over the wire, and the whole tree in one round trip.

Every element of a value is a value in its own right, so walking an
attribute set from a client is a chain of handles - a round trip and a
thread handover per node. Realize walks it once, on the value's own
thread, and answers with the shape (tasks/030).
"""

from typing import Any

import pytest
from conftest import HOST, Server

from huggorm import remote
from huggorm_generated import RPCValue


@pytest.fixture
async def state(server: Server) -> Any:
    """A fresh evaluator per test, on its own connection, so one test's
    handles never outlive it into another's assertions."""
    c = await remote.connect(HOST, server.port)
    s = await c.acquire("EvalState", "dummy://")
    yield s
    await s.aclose()
    c.stop_pinging()


async def bag(state: Any) -> Any:
    """{ apple = "first"; xs = [ 7 ]; zebra = 1; }"""
    attrs = await state.make_attrs()
    await state.attrs_set(attrs, "zebra", await state.make_int(1))
    await state.attrs_set(attrs, "apple", await state.make_string("first"))
    xs = await state.make_list()
    await state.list_append(xs, await state.make_int(7))
    await state.attrs_set(attrs, "xs", xs)
    return attrs


# -- walking it one call at a time -----------------------------------------

async def test_an_attribute_set_crosses_as_a_proxy(state: Any) -> None:
    attrs = await bag(state)
    assert await attrs.type_name() == "attrs"
    assert await attrs.size() == 3


async def test_attribute_names_come_back_alphabetical(state: Any) -> None:
    """Nix attribute sets are alphabetical, and the order survives the
    wire because it is the storage order, not a detail of the
    message."""
    attrs = await bag(state)
    names = [await attrs.name_at(i) for i in range(3)]
    assert names == ["apple", "xs", "zebra"], names


async def test_an_attribute_value_is_a_handle_of_its_own(state: Any) -> None:
    attrs = await bag(state)
    assert await (await attrs.get("apple")).string_value() == "first"
    assert await (await (await attrs.get("xs")).at(0)).integer() == 7


# -- one round trip --------------------------------------------------------

async def test_realize_returns_the_shape(state: Any) -> None:
    client = state._client
    tree = await client.realize(await bag(state))
    assert tree == {"apple": "first", "xs": [7], "zebra": 1}, tree
    assert list(tree) == ["apple", "xs", "zebra"], "still alphabetical"


async def test_realize_forces_nothing(state: Any) -> None:
    """A thunk is exactly what cannot be serialized, so it crosses as a
    proxy and the caller forces it with the call that already exists."""
    client = state._client
    lazy = await state.make_attrs()
    await state.attrs_set(lazy, "later", await state.parse_expr("42"))
    held = await client.realize(lazy)
    assert isinstance(held["later"], RPCValue), type(held["later"]).__name__

    await state.force(held["later"])
    assert await client.realize(lazy) == {"later": 42}


async def test_depth_bounds_the_walk(state: Any) -> None:
    """depth counts levels EXPANDED, so 1 is the root alone. Zero would
    be the natural spelling for that and proto3 cannot tell a zero from
    an unset field."""
    client = state._client
    attrs = await bag(state)

    flat = await client.realize(attrs, depth=1)
    assert set(flat) == {"apple", "xs", "zebra"}, flat
    assert all(isinstance(v, RPCValue) for v in flat.values()), flat

    shallow = await client.realize(attrs, depth=2)
    assert shallow["zebra"] == 1
    assert shallow["apple"] == "first"
    assert isinstance(shallow["xs"][0], RPCValue), shallow


async def test_budget_bounds_the_walk_sideways(state: Any) -> None:
    """The bound that actually bites: an attribute set can hold a
    hundred thousand entries one level down, which no depth limit
    touches."""
    client = state._client
    wide = await state.make_attrs()
    for i in range(20):
        await state.attrs_set(wide, f"k{i:02d}", await state.make_int(i))

    # 1 for the root, then 3 children before it runs out.
    capped = await client.realize(wide, budget=4)
    kept = [k for k, v in capped.items() if not isinstance(v, RPCValue)]
    assert len(kept) == 3, kept
    assert await capped["k19"].integer() == 19, "the rest is still reachable"


async def test_a_repeated_value_crosses_once(state: Any) -> None:
    """Values are immutable and shared freely, so without visit
    tracking a diamond is copied and a cycle never ends. The repeated
    position still carries a handle, and identity mapping makes it the
    same handle - so the sharing survives rather than being flattened
    away."""
    client = state._client
    shared = await state.make_list()
    twice = await state.make_attrs()
    await state.attrs_set(twice, "a", shared)
    await state.attrs_set(twice, "b", shared)

    diamond = await client.realize(twice)
    expanded = [k for k, v in diamond.items() if not isinstance(v, RPCValue)]
    assert expanded == ["a"] and diamond["a"] == [], diamond
