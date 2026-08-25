"""
Handle lifetime over real gRPC (tasks/002, 028, 031, 016).

Every path a handle can take between processes: bind and claim, leases
and capability, producer pinning with cascading reaps, share as copy
and as transfer, detach into escrow surviving connection death, TTL
reaping, and a client-side drop handing the lease back.

The sweeper is slow on purpose, so the tests that have to outlast it
share one wait: the `swept` fixture sets everything up, waits once, and
each test asserts against what survived. Doing it per test would be a
minute of sleeping.
"""

import gc
from dataclasses import dataclass
from typing import Any

import anyio
import pytest
from conftest import HOST, SHORT_TTL, Server

from cythonix import remote

pytestmark = pytest.mark.anyio


async def wrapper_error(coro: Any) -> dict[str, str]:
    """Run something expected to fail, and return the typed error it
    crossed the wire as. Errors travel as JSON in the gRPC status, so
    the cause type survives the hop and can be asserted on."""
    from cythonix_generated._runtime import InternalError

    try:
        await coro
    except InternalError as e:
        return e.to_dict()
    raise AssertionError("expected the call to fail")


# -- capability and leases -------------------------------------------------

async def test_distinct_clients_get_distinct_tokens(ttl_server: Server) -> None:
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    assert a.token != b.token
    a.stop_pinging()
    b.stop_pinging()


async def test_a_handle_is_a_capability(ttl_server: Server) -> None:
    """Anyone holding the id may call. Lifetime is what tokens govern."""
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    store = await a.acquire("LocalStore")
    cross = b.proxy("LocalStore", store.handle_id)
    assert await cross.get_uri() == "local"
    a.stop_pinging()
    b.stop_pinging()


async def test_naming_a_handle_makes_you_a_holder(ttl_server: Server) -> None:
    """Two processes share one object by passing its id between them
    however they like: the second calls, and the object stays alive for
    it without the first arranging anything (tasks/031)."""
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    shared = await a.acquire("LocalStore")
    hid = shared.handle_id
    borrowed = b.proxy("LocalStore", hid)
    assert await borrowed.get_uri() == "local"

    # Calling again owes no second release. A lease that counted calls
    # would be one no client could balance: a client releases once per
    # client-side object, not once per call.
    await borrowed.get_uri()
    await borrowed.get_uri()
    await a.release(shared)
    assert await borrowed.get_uri() == "local", "outlives its acquirer"

    await b.release(borrowed)
    gone = await wrapper_error(b.proxy("LocalStore", hid).get_uri())
    assert gone["cause_type"] == "KeyError", gone
    a.stop_pinging()
    b.stop_pinging()


async def test_double_release_fails_typed(ttl_server: Server) -> None:
    """Releasing the same handle twice, through a FRESH object each
    time. Reusing the spent one sent an empty id, so the old test
    asserted that releasing handle "" fails - which proves nothing."""
    a = await remote.connect(HOST, ttl_server.port)
    store = await a.acquire("LocalStore")
    hid = store.handle_id
    await a.release(store)
    again = a.proxy("LocalStore", hid)
    threw = await wrapper_error(a.release(again))
    assert threw["cause_type"] == "ValueError", threw
    assert hid[:8] in threw["cause_message"], threw

    with pytest.raises(ValueError, match="already released"):
        await a.release(store)  # blanked client-side
    a.stop_pinging()


# -- share -----------------------------------------------------------------

async def test_share_copy_survives_the_granter(ttl_server: Server) -> None:
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    assert b.token is not None  # connect() binds
    store = await a.acquire("LocalStore")
    hid = store.handle_id
    await a.share(store, b.token, mode="copy")
    await a.release(store)
    assert await b.proxy("LocalStore", hid).get_uri() == "local"
    a.stop_pinging()
    b.stop_pinging()


async def test_share_transfer_moves_ownership(ttl_server: Server) -> None:
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    assert b.token is not None
    store = await a.acquire("LocalStore")
    hid = store.handle_id
    await a.share(store, b.token, mode="transfer")
    threw = await wrapper_error(a.release(store))
    assert threw["cause_type"] == "ValueError", threw
    assert await b.proxy("LocalStore", hid).get_uri() == "local"
    a.stop_pinging()
    b.stop_pinging()


# -- producer pinning ------------------------------------------------------

async def test_producer_pinning_and_cascade_reap(ttl_server: Server) -> None:
    """A child pins the producer that made it, and dropping the child
    frees both."""
    a = await remote.connect(HOST, ttl_server.port)
    rstore = await a.acquire("RemoteStore")
    drv = await rstore.query_derivation(
        await rstore.add_text_to_store("life.drv", "DrvLife"))
    assert "seen 1x" in await drv.describe()

    await a.release(rstore)
    assert "seen 2x" in await drv.describe(), "child keeps the producer alive"

    hid_drv = drv.handle_id
    await a.release(drv)
    gone = await wrapper_error(a.proxy("Derivation", hid_drv).describe())
    assert gone["cause_type"] == "KeyError", gone
    a.stop_pinging()


# -- everything that has to outlast the sweeper ----------------------------

@dataclass
class Swept:
    """What was set up before the sweep, and survived it (or did not)."""

    port: int
    escrow_token: str
    thunk_id: str
    maker_token: str
    state_id: str
    bag_id: str
    lazy_id: str
    doomed_id: str
    alive: Any


@pytest.fixture(scope="session")
async def swept(ttl_server: Server) -> Any:
    """Set up everything that needs a sweep, then wait once.

    Three scenarios share the wait: leases detached into escrow, an
    evaluation state handed over to a successor, and a connection
    abandoned without detaching. A pinging client is kept alive across
    it as the control."""
    # 1. detached leases, whose owner then dies.
    a = await remote.connect(HOST, ttl_server.port)
    state = await a.acquire("EvalState", "local")
    thunk = await state.parse_expr("42")
    await state.force(thunk)
    thunk_id = thunk.handle_id
    assert await a.detach(all=True), "detach reports moved leases"
    threw = await wrapper_error(a.release(thunk))
    assert threw["cause_type"] == "ValueError", "detached leases are not ours"
    assert await thunk.integer() == 42, "detached handles stay callable"
    a.stop_pinging()

    # 2. an evaluation state, with work done, handed to a successor.
    maker = await remote.connect(HOST, ttl_server.port)
    warm = await maker.acquire("EvalState", "local")
    bag = await warm.make_attrs()
    await warm.attrs_set(bag, "answer", await warm.eval_expr("42"))
    lazy = await warm.parse_expr("7")
    assert await lazy.type_name() == "thunk"
    await warm.force(lazy)
    state_id, bag_id, lazy_id = warm.handle_id, bag.handle_id, lazy.handle_id
    assert await maker.detach(all=True)
    maker.stop_pinging()

    # 3. a connection abandoned WITHOUT detaching: its handles go.
    d = await remote.connect(HOST, ttl_server.port)
    doomed = await d.acquire("LocalStore")
    doomed_id = doomed.handle_id
    d.stop_pinging()

    # 4. the control: a client that keeps pinging is immune.
    live = await remote.connect(HOST, ttl_server.port)
    alive = await live.acquire("LocalStore")

    # connect() binds, so both tokens exist from that call onward.
    assert a.token is not None and maker.token is not None

    await anyio.sleep(SHORT_TTL * 1.5 + 1.0)
    yield Swept(ttl_server.port, a.token, thunk_id, maker.token,
                state_id, bag_id, lazy_id, doomed_id, alive)
    live.stop_pinging()


async def test_pinging_client_survives_the_sweeper(swept: Swept) -> None:
    assert await swept.alive.get_uri() == "local"


async def test_abandoned_handles_are_reaped(swept: Swept) -> None:
    c = await remote.connect(HOST, swept.port)
    gone = await wrapper_error(c.proxy("LocalStore", swept.doomed_id).get_uri())
    assert gone["cause_type"] == "KeyError", gone
    c.stop_pinging()


async def test_escrow_survives_connection_death(swept: Swept) -> None:
    """Escrow is deliberately untouched by the sweep: detached leases
    are unowned and never auto-reaped, which is what lets a creator
    exit entirely while its objects wait for a claim."""
    c = await remote.connect(HOST, swept.port, claim=swept.escrow_token)
    assert c.token == swept.escrow_token, "claim adopts the detached token"
    assert await c.proxy("Value", swept.thunk_id).integer() == 42
    c.stop_pinging()


async def test_a_claimed_lease_is_a_normal_lease(swept: Swept) -> None:
    """Regression guard for the escrow double count, where Bind ADDED a
    lease instead of moving the escrowed one back - so no number of
    releases ever reached zero and every detach/claim round trip leaked
    its handle for good."""
    c = await remote.connect(HOST, swept.port, claim=swept.escrow_token)
    claimed = c.proxy("Value", swept.thunk_id)
    await c.release(claimed)
    gone = await wrapper_error(c.proxy("Value", swept.thunk_id).integer())
    assert gone["cause_type"] == "KeyError", gone
    c.stop_pinging()


async def test_the_evaluator_outlives_its_creator(swept: Swept) -> None:
    """The vision the whole lifecycle exists for (tasks/016): one
    EvalState serving many connections over time."""
    heir = await remote.connect(HOST, swept.port, claim=swept.maker_token)
    assert heir.token == swept.maker_token, "the successor adopts the identity"
    same = heir.proxy("EvalState", swept.state_id)
    assert await same.get_store_uri() == "local"

    # Warm, not rebuilt. Forcing is the proof: it mutates a value in
    # place, so a value that reads as an int on the far side of a
    # handover is the one that was forced before it.
    assert await heir.proxy("Value", swept.lazy_id).type_name() == "int"
    assert await heir.realize(heir.proxy("Value", swept.bag_id)) == {"answer": 42}
    fresh = await same.eval_expr('"after the handover"')
    assert await fresh.string_value() == "after the handover"
    heir.stop_pinging()


# -- a dropped client object releases its lease (tasks/028) ----------------
# These need no sweep: a pinging connection is immune to the sweeper,
# which is exactly why they matter. Before this, the only way such a
# connection ever gave a handle back was an explicit release, so every
# proxy it was granted stayed alive for the life of the connection.

async def test_dropping_the_last_reference_releases(client: Any) -> None:
    state = await client.acquire("EvalState", "local")
    ids = []
    for i in range(10):
        v = await state.eval_expr(f'"v{i}"')
        ids.append(v.handle_id)
        del v
    gc.collect()
    assert len(client._dropped) == 10, client._dropped
    assert await client.flush_dropped() == 10, "one rpc for the whole batch"
    for hid in ids:
        await wrapper_error(client.proxy("Value", hid).string_value())
    await state.aclose()


async def test_two_objects_one_handle(client: Any) -> None:
    """The first drop must not pull the lease out from under the
    second. Forging a proxy from a raw id is public API, and this file
    does it a dozen times."""
    state = await client.acquire("EvalState", "local")
    v = await state.eval_expr('"shared"')
    shared_id = v.handle_id
    twin = client.proxy("Value", shared_id)
    del v
    gc.collect()
    await client.flush_dropped()
    assert await twin.string_value() == "shared"

    del twin
    gc.collect()
    await client.flush_dropped()
    await wrapper_error(client.proxy("Value", shared_id).string_value())
    await state.aclose()


async def test_reacquired_before_the_flush_is_not_released(client: Any) -> None:
    """Releasing a queued handle that something started using again
    would take the lease from a live object."""
    state = await client.acquire("EvalState", "local")
    v = await state.eval_expr('"resurrected"')
    res_id = v.handle_id
    del v
    gc.collect()
    again = client.proxy("Value", res_id)
    assert await client.flush_dropped() == 0
    assert await again.string_value() == "resurrected"
    await again.aclose()
    await state.aclose()


async def test_explicit_close_does_not_queue_twice(client: Any) -> None:
    state = await client.acquire("EvalState", "local")
    v = await state.eval_expr('"closed"')
    await v.aclose()
    del v
    gc.collect()
    assert client._dropped == [], client._dropped
    await state.aclose()
