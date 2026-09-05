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

from huggorm import remote
from huggorm_bindings.errors import SysError


async def wrapper_error(coro: Any) -> dict[str, str]:
    """Run something expected to fail, and return the typed error it
    crossed the wire as. Errors travel as JSON in the gRPC status, so
    the cause type survives the hop and can be asserted on."""
    from huggorm_generated._runtime import InternalError

    try:
        await coro
    except InternalError as e:
        return e.to_dict()
    raise AssertionError("expected the call to fail")


# -- capability and leases -------------------------------------------------
#
# `Store("dummy://")` is the subject, and the URI is load-bearing.
#
# These tests are about LEASES - who holds a handle, what pins it, when
# it is reaped - and the object behind the handle is incidental. It
# used to be a mock store, which is one of the things the mock was
# for; a real store is a better subject because a lease over a thing
# that actually holds resources is the case that matters.
#
# But NOT a real LocalStore on one tmp_path. `declare.py` records why
# nanopynix grew a per-state-directory cache: two LocalStores in one
# process deadlock on a temp-roots flock. This suite acquires a store
# many times across many connections, which is exactly that shape.
# `dummy://` is in-memory and takes no lock, so it is the one real
# store this suite can hold many of.
#
# If a test here ever needs a store with a FILESYSTEM, give it its own
# tmp_path rather than sharing one. Found in review before it hung
# anything, not after.

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
    store = await a.acquire("Store", "dummy://")
    cross = b.proxy("Store", store.handle_id)
    assert await cross.get_uri() == "dummy://"
    a.stop_pinging()
    b.stop_pinging()


async def test_naming_a_handle_makes_you_a_holder(ttl_server: Server) -> None:
    """Two processes share one object by passing its id between them
    however they like: the second calls, and the object stays alive for
    it without the first arranging anything (tasks/031)."""
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    shared = await a.acquire("Store", "dummy://")
    hid = shared.handle_id
    borrowed = b.proxy("Store", hid)
    assert await borrowed.get_uri() == "dummy://"

    # Calling again owes no second release. A lease that counted calls
    # would be one no client could balance: a client releases once per
    # client-side object, not once per call.
    await borrowed.get_uri()
    await borrowed.get_uri()
    await a.release(shared)
    assert await borrowed.get_uri() == "dummy://", "outlives its acquirer"

    await b.release(borrowed)
    gone = await wrapper_error(b.proxy("Store", hid).get_uri())
    assert gone["cause_type"] == "KeyError", gone
    a.stop_pinging()
    b.stop_pinging()


async def test_double_release_fails_typed(ttl_server: Server) -> None:
    """Releasing the same handle twice, through a FRESH object each
    time. Reusing the spent one sent an empty id, so the old test
    asserted that releasing handle "" fails - which proves nothing."""
    a = await remote.connect(HOST, ttl_server.port)
    store = await a.acquire("Store", "dummy://")
    hid = store.handle_id
    await a.release(store)
    again = a.proxy("Store", hid)
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
    store = await a.acquire("Store", "dummy://")
    hid = store.handle_id
    await a.share(store, b.token, mode="copy")
    await a.release(store)
    assert await b.proxy("Store", hid).get_uri() == "dummy://"
    a.stop_pinging()
    b.stop_pinging()


async def test_share_transfer_moves_ownership(ttl_server: Server) -> None:
    a = await remote.connect(HOST, ttl_server.port)
    b = await remote.connect(HOST, ttl_server.port)
    assert b.token is not None
    store = await a.acquire("Store", "dummy://")
    hid = store.handle_id
    await a.share(store, b.token, mode="transfer")
    threw = await wrapper_error(a.release(store))
    assert threw["cause_type"] == "ValueError", threw
    assert await b.proxy("Store", hid).get_uri() == "dummy://"
    a.stop_pinging()
    b.stop_pinging()


# -- producer pinning ------------------------------------------------------

async def test_producer_pinning_and_cascade_reap(ttl_server: Server) -> None:
    """A child pins the producer that made it, and dropping the child
    frees both.

    EvalState and Value are the pair this is about. A Value is GC
    memory the state owns, so a state reaped while a value still
    points into it is a use-after-free, not a lost handle. Every other
    producer in this repo hands back a wire VALUE, which needs no
    pinning at all - so this test is the only exercise the pinning,
    the cascade and the adopt path get."""
    a = await remote.connect(HOST, ttl_server.port)
    state = await a.acquire("EvalState", "dummy://")
    hid_state = state.handle_id
    v = await state.make_int(42)
    assert await v.integer() == 42

    await a.release(state)
    assert await v.integer() == 42, "child keeps the producer alive"

    hid_v = v.handle_id
    await a.release(v)
    for cls, hid in (("Value", hid_v), ("EvalState", hid_state)):
        gone = await wrapper_error(a.proxy(cls, hid).is_gc_managed()
                                   if cls == "Value"
                                   else a.proxy(cls, hid).get_store_uri())
        assert gone["cause_type"] == "KeyError", (cls, gone)
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
    # A path the warm state evaluated and that no longer EXISTS. The
    # milestone in CLAUDE.md is that a claimed state answers for it
    # anyway, and that is only meaningful because a fresh one cannot.
    warm_file: str
    doomed_id: str
    doomed_client: Any
    alive: Any


@pytest.fixture(scope="session")
async def swept(ttl_server: Server, tmp_path_factory: Any) -> Any:
    """Set up everything that needs a sweep, then wait once.

    Three scenarios share the wait: leases detached into escrow, an
    evaluation state handed over to a successor, and a connection
    abandoned without detaching. A pinging client is kept alive across
    it as the control."""
    # 1. detached leases, whose owner then dies.
    a = await remote.connect(HOST, ttl_server.port)
    state = await a.acquire("EvalState", "dummy://")
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
    warm = await maker.acquire("EvalState", "dummy://")
    bag = await warm.make_attrs()
    await warm.attrs_set(bag, "answer", await warm.eval_expr("42"))
    lazy = await warm.parse_expr("7")
    assert await lazy.type_name() == "thunk"
    await warm.force(lazy)
    # ...and a FILE, which is the only warm thing libexpr keeps by
    # itself. `evalFile` caches by resolved path, so deleting the file
    # here leaves the cache as the only way to answer for it
    # (eval.cc:1118, and tasks/016).
    warm_file = tmp_path_factory.mktemp("warm") / "answer.nix"
    warm_file.write_text("40 + 2\n")
    assert await (await warm.eval_file(str(warm_file))).integer() == 42
    warm_file.unlink()
    state_id, bag_id, lazy_id = warm.handle_id, bag.handle_id, lazy.handle_id
    assert await maker.detach(all=True)
    maker.stop_pinging()

    # 3. a connection abandoned WITHOUT detaching: its handles go.
    d = await remote.connect(HOST, ttl_server.port)
    doomed = await d.acquire("Store", "dummy://")
    doomed_id = doomed.handle_id
    d.stop_pinging()

    # 4. the control: a client that keeps pinging is immune.
    live = await remote.connect(HOST, ttl_server.port)
    alive = await live.acquire("Store", "dummy://")

    # connect() binds, so both tokens exist from that call onward.
    assert a.token is not None and maker.token is not None

    await anyio.sleep(SHORT_TTL * 1.5 + 1.0)
    yield Swept(ttl_server.port, a.token, thunk_id, maker.token,
                state_id, bag_id, lazy_id, str(warm_file), doomed_id, d, alive)
    live.stop_pinging()


async def test_pinging_client_survives_the_sweeper(swept: Swept) -> None:
    assert await swept.alive.get_uri() == "dummy://"


async def test_ping_reports_a_swept_connection(swept: Swept) -> None:
    """`ok` finally means something.

    Ping used to resolve the token through a lookup that CREATES on a
    miss, so a client the sweeper had already reaped got an empty
    connection back under its old token and a cheerful ok=True. It
    kept pinging happily and discovered its death later, as "unknown
    handle" on some unrelated call - the one moment nobody is looking
    for a lifecycle bug (tasks/049).

    A fresh connection still works, which is what says the server
    refused this token rather than the service."""
    from huggorm.grpc_pb import PKG

    c = swept.doomed_client
    ack = await c._rpc(f"/{PKG}.Session/Ping", c.msg("PingReq")(), "AckResp")
    assert ack.ok is False

    fresh = await remote.connect(HOST, swept.port)
    ok = await fresh._rpc(
        f"/{PKG}.Session/Ping", fresh.msg("PingReq")(), "AckResp")
    assert ok.ok is True
    fresh.stop_pinging()


async def test_a_swept_client_stops_rather_than_rebinding(
        swept: Swept) -> None:
    """What the client DOES about it, which is the decision this task
    actually carried.

    Not an automatic re-bind. The leases went with the sweep, so every
    handle the client holds is dead; a fresh bind would hand back a
    live-looking client whose every call fails, which is the same late
    confusing failure moved one step. So it stops, says so once, and
    the next call raises.

    Recovery is bind() plus re-acquiring, and that is the caller's
    decision because only the caller knows what it was holding."""
    c = swept.doomed_client
    assert c._expired is False, "nothing has told it yet"

    # One iteration of the real loop, which returns as soon as it
    # learns the answer.
    with anyio.fail_after(5):
        await c._ping_loop(0.01)
    assert c._expired is True

    with pytest.raises(remote.ConnectionExpired, match="swept"):
        await c.acquire("Store", "dummy://")


async def test_abandoned_handles_are_reaped(swept: Swept) -> None:
    c = await remote.connect(HOST, swept.port)
    gone = await wrapper_error(c.proxy("Store", swept.doomed_id).get_uri())
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
    assert await same.get_store_uri() == "dummy://"

    # Warm, not rebuilt. Forcing is the proof: it mutates a value in
    # place, so a value that reads as an int on the far side of a
    # handover is the one that was forced before it.
    assert await heir.proxy("Value", swept.lazy_id).type_name() == "int"
    assert await heir.realize(heir.proxy("Value", swept.bag_id)) == {"answer": 42}
    fresh = await same.eval_expr('"after the handover"')
    assert await fresh.string_value() == "after the handover"
    heir.stop_pinging()


async def test_a_claimed_state_answers_for_a_file_it_can_no_longer_read(
        swept: Swept) -> None:
    """The milestone, as `CLAUDE.md` words it: a second client claims a
    live EvalState, and re-evaluating unchanged input does no
    re-evaluation.

    The creator evaluated a file and then DELETED it. libexpr caches
    an evaluation by resolved path and a hit never opens the file, so
    the claimed state can still answer - and nothing else can. That is
    what makes the handover worth having: a state that dies takes the
    warm work with it, so restarting is not a slower way to the same
    place.

    The control is in the same test, on the same server, in the same
    moment. A FRESH EvalState asked for the same path goes to disk and
    says so."""
    heir = await remote.connect(HOST, swept.port, claim=swept.maker_token)
    same = heir.proxy("EvalState", swept.state_id)

    warm = await same.eval_file(swept.warm_file)
    assert await warm.integer() == 42, "the claimed state still has it"

    cold = await heir.acquire("EvalState", "dummy://")
    # `SysError`, not the wrapper: a declared Nix error crosses as
    # ITSELF (tasks/066), so `wrapper_error` - which catches only
    # InternalError - does not see this one. Measured by writing it
    # that way first and watching the SysError go straight through.
    with pytest.raises(SysError) as caught:
        await cold.eval_file(swept.warm_file)
    # "opening file" is the cold state SAYING it went to disk, which is
    # the half the claimed state is claimed not to do.
    assert "opening file" in str(caught.value)
    assert "answer.nix" in str(caught.value)
    heir.stop_pinging()


# -- a dropped client object releases its lease (tasks/028) ----------------
# These need no sweep: a pinging connection is immune to the sweeper,
# which is exactly why they matter. Before this, the only way such a
# connection ever gave a handle back was an explicit release, so every
# proxy it was granted stayed alive for the life of the connection.

async def test_dropping_the_last_reference_releases(client: Any) -> None:
    state = await client.acquire("EvalState", "dummy://")
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
    state = await client.acquire("EvalState", "dummy://")
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
    state = await client.acquire("EvalState", "dummy://")
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
    state = await client.acquire("EvalState", "dummy://")
    v = await state.eval_expr('"closed"')
    await v.aclose()
    del v
    gc.collect()
    assert client._dropped == [], client._dropped
    await state.aclose()
