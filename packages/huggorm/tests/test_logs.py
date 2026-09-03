"""Nix telling Python what it is doing.

The other direction, like `test_primop.py`, and without a call. A
record is raised inside the work - on the evaluation thread, in the
middle of an evaluation - and reaches a queue rather than a callback.

The queue is the design. A callback there would take the GIL once per
log line and run arbitrary Python inside the evaluator; `push` takes a
mutex and returns. So these tests drain, they never wait.

Two things every gate here has to keep in mind, both measured against
`logging.hh` and recorded in `tasks/032`:

- the global `nix::verbosity` filters BEFORE any logger runs, so a
  subscription level can only narrow;
- an ACTIVITY is not filtered at all. `Activity::Activity` calls
  `startActivity` with no test, so a start arrives whatever its level
  says.
"""

import pathlib
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from conftest import HOST, SHORT_TTL, Server

URI = "dummy://"

# builtins.trace goes through printError, which is lvlError - 0, and
# under every verbosity there is. So it is the one message a test can
# rely on arriving.
TRACE = 'builtins.trace "%s" 1'

# builtins.warn takes the OTHER path: logWarning, so logEI rather than
# log, and lvlWarn.
WARN = 'builtins.warn "%s" 1'


@pytest.fixture
def state() -> Any:
    from huggorm_bindings import EvalState

    return EvalState(URI)


@pytest.fixture
def subscribed(state: Any) -> Iterator[tuple[Any, Any]]:
    """A state and a queue, and the thread left clean afterwards.

    The subscription belongs to the THREAD, so a test that left one
    behind would be recording into the next test's queue. That is not
    hypothetical: pytest runs these in one thread."""
    stream = state.subscribe_logs()
    yield state, stream
    state.unsubscribe_logs()


def texts(stream: Any) -> list[str]:
    return [r.text() for r in stream.drain()]


def test_a_trace_arrives_as_a_message(subscribed: tuple[Any, Any]) -> None:
    """The whole thing, in three lines.

    `builtins.trace` calls `printError` (`primops.cc:1282`), which is
    `printMsg(lvlError, ...)`. So the record is a "msg" at level 0,
    and the text is Nix's own - the binding renders nothing."""
    state, stream = subscribed
    state.eval_expr(TRACE % "hello")

    records = stream.drain()
    assert [r.action() for r in records] == ["msg"]
    assert records[0].level() == 0
    assert "trace: hello" in records[0].text()
    # A message belongs to no activity, and 0 is how Nix spells that.
    assert records[0].id() == 0


def test_a_warning_takes_the_other_path(subscribed: tuple[Any, Any]) -> None:
    """`logEI`, not `log`, and it arrives rendered.

    `builtins.warn` builds an `ErrorInfo` and calls `logWarning`
    (`primops.cc:1324`), so this drives the second of the five
    virtuals. An `ErrorInfo` carries a trace of positions, and what
    crosses is what `JSONLogger` would have written: the rendering."""
    state, stream = subscribed
    state.eval_expr(WARN % "careful")

    records = stream.drain()
    assert [r.action() for r in records] == ["msg"]
    assert records[0].level() == 1, "lvlWarn"
    assert "careful" in records[0].text()


def test_the_level_refuses_what_it_did_not_ask_for(state: Any) -> None:
    """A subscription can narrow, and only narrow.

    Level 0 is lvlError, which the trace is. The warning is lvlWarn,
    which is 1, so it does not arrive. Both were RAISED - the queue is
    what refuses one, and `dropped` does not move for it, because that
    counter is the BOUND's rather than the filter's."""
    stream = state.subscribe_logs(level=0)
    try:
        state.eval_expr(TRACE % "kept")
        state.eval_expr(WARN % "refused")
        seen = " ".join(texts(stream))
    finally:
        state.unsubscribe_logs()
    assert "kept" in seen
    assert "refused" not in seen


def test_nothing_is_recorded_without_a_subscription(state: Any) -> None:
    """A tap that is always installed must cost nothing when unused.

    The tap goes under `nix::logger` at import, whether or not anybody
    subscribes. If it recorded anyway, the work between the
    `unsubscribe_logs` and the second `subscribe_logs` would show up
    here."""
    state.subscribe_logs()
    state.unsubscribe_logs()

    state.eval_expr(TRACE % "unheard")

    stream = state.subscribe_logs()
    try:
        assert stream.drain() == []
    finally:
        state.unsubscribe_logs()


def test_a_drain_empties_the_queue(subscribed: tuple[Any, Any]) -> None:
    """Drained records are gone, so a reader never sees one twice."""
    state, stream = subscribed
    state.eval_expr(TRACE % "once")

    assert len(stream.drain()) == 1
    assert stream.drain() == []


def test_a_full_queue_drops_a_message_and_counts_it(state: Any) -> None:
    """The bound, and the number that says it was reached.

    A dropped log line is usually the right answer and a silent one is
    not: a reader that cannot tell "nothing happened" from "I could
    not keep up" is looking at this repo's named failure mode.
    `dropped` is what makes those two different answers."""
    stream = state.subscribe_logs(capacity=2)
    try:
        for i in range(5):
            state.eval_expr(TRACE % f"line{i}")
        records = stream.drain()
        dropped = stream.dropped()
    finally:
        state.unsubscribe_logs()

    assert len(records) == 2, "the bound held"
    assert dropped == 3, "and said how much it refused"
    # The EARLIEST survive. A full queue refuses the incoming record
    # rather than evicting one, because evicting means scanning for
    # the oldest DROPPABLE record - a stop may not be evicted - and
    # that scan is paid on the evaluation thread.
    assert "line0" in records[0].text()


def test_the_drop_count_is_cumulative(state: Any) -> None:
    """It counts over the queue's life, not since the last drain.

    A reader that misses a drain still sees the number grow, which is
    what lets it notice at all."""
    stream = state.subscribe_logs(capacity=1)
    try:
        state.eval_expr(TRACE % "a")
        state.eval_expr(TRACE % "b")
        stream.drain()
        first = stream.dropped()
        state.eval_expr(TRACE % "c")
        state.eval_expr(TRACE % "d")
        second = stream.dropped()
    finally:
        state.unsubscribe_logs()

    assert first == 1
    assert second == 2, "cumulative, not reset by the drain"


def test_a_second_subscription_replaces_the_first(state: Any) -> None:
    """One thread, one queue.

    Two live subscriptions on one thread would each get an arbitrary
    half of the records, which is worse for both readers than one of
    them getting none. So subscribing again closes what was there."""
    first = state.subscribe_logs()
    second = state.subscribe_logs()
    try:
        state.eval_expr(TRACE % "after")
        assert first.drain() == [], "the replaced queue stopped filling"
        assert len(second.drain()) == 1
    finally:
        state.unsubscribe_logs()


def test_a_subscription_belongs_to_the_thread_not_the_state(state: Any) -> None:
    """Named because it is a limitation, not a feature.

    The tap routes by THREAD, which is right for the affine model this
    repo has - one EvalState per thread - and it means two states on
    ONE thread share a subscription. The second `subscribe_logs`
    closes the first's queue even though a different object asked.

    Sound where a state has its own thread, which is how the async
    layer runs one. `tasks/032` records it rather than hiding it."""
    from huggorm_bindings import EvalState

    other = EvalState(URI)
    mine = state.subscribe_logs()
    theirs = other.subscribe_logs()
    try:
        state.eval_expr(TRACE % "whose")
        assert mine.drain() == []
        assert len(theirs.drain()) == 1, "the thread's queue, not the state's"
    finally:
        other.unsubscribe_logs()


def test_close_stops_a_queue_filling(subscribed: tuple[Any, Any]) -> None:
    """A reader that goes away must not leave the evaluator recording.

    `close` is that, from the queue's side. The subscription on the
    thread is a separate fact and `unsubscribe_logs` is what clears
    it, which is why this is still callable afterwards."""
    state, stream = subscribed
    stream.close()

    state.eval_expr(TRACE % "ignored")
    assert stream.drain() == []


def test_a_record_carries_its_fields_as_a_list(subscribed: tuple[Any, Any]) -> None:
    """`fields` is a list of `LogField`, not a list of strings.

    A field is an integer OR a string - upstream's own hand-rolled
    variant (`logging.hh:76`) - and a progress result is made of
    numbers. Flattening one to its rendering would lose the difference
    between the number 42 and the string "42"."""
    state, stream = subscribed
    state.eval_expr(TRACE % "plain")

    record = stream.drain()[0]
    assert record.fields() == [], "a message carries none"
    assert isinstance(record.fields(), list)


@pytest.mark.live
def test_an_activity_arrives_as_a_start_and_a_stop(
        tmp_path: pathlib.Path) -> None:
    """The tree, not the stream.

    Interpolating a path copies it into the store, and `fetchToStore`
    raises an activity around that (`fetch-to-store.cc:74`). So one
    eval produces a start and the stop that closes it.

    Live: it writes to the ambient store, and a build sandbox has
    none. `dummy://` cannot answer it either, so this state is opened
    against "auto" rather than the fixture's."""
    from huggorm_bindings import EvalState

    (tmp_path / "f").write_text("hi\n")
    real = EvalState("auto")
    stream = real.subscribe_logs()
    try:
        real.eval_expr(f'"${{{tmp_path}}}"')
        records = stream.drain()
    finally:
        real.unsubscribe_logs()

    actions = [r.action() for r in records]
    assert "start" in actions, actions
    assert "stop" in actions, actions
    starts = [r for r in records if r.action() == "start"]
    stops = {r.id() for r in records if r.action() == "stop"}
    assert starts[0].id() in stops, "the start this stop closes"
    assert starts[0].id() != 0, "an activity has an id"


@pytest.mark.live
def test_a_stop_is_never_dropped(tmp_path: pathlib.Path) -> None:
    """The asymmetry the bound exists to respect.

    A dropped message costs a reader one line. A dropped stop costs it
    a node in the activity tree that nothing later closes - so the
    reader's progress display keeps a task that finished long ago,
    forever. Capacity 1 is not a reason to do that.

    Perturbation: make `push` treat "stop" as droppable and this
    fails while every other gate here still passes."""
    from huggorm_bindings import EvalState

    (tmp_path / "f").write_text("hi\n")
    real = EvalState("auto")
    stream = real.subscribe_logs(capacity=1)
    try:
        real.eval_expr(f'"${{{tmp_path}}}"')
        records = stream.drain()
    finally:
        real.unsubscribe_logs()

    starts = [r.id() for r in records if r.action() == "start"]
    stops = [r.id() for r in records if r.action() == "stop"]
    assert starts, "an activity was raised"
    assert sorted(starts) == sorted(stops), \
        "every start past the bound still got its stop"


def test_no_rpc_surface() -> None:
    """A log stream does not cross the wire yet, and says so.

    `LogStream` is a PROXY - it crosses as a handle - and it is not
    wrapped, because it is pool-threaded and no method of it blocks.
    So nothing publishes a service for that handle, and a remote
    `subscribe_logs` would answer an id no later call could use.

    That was SHIPPING before this gate. The schema had
    `EvalState/subscribe_logs` and no `LogStreamService`, and nothing
    complained - `grpc_schema.annotate` assumed an unwrapped class
    crosses by copy, which was true of every unwrapped class until
    this one. The refusal is derived now: a return whose type is a
    proxy with no service is a `wire_blocker`, so any future case
    reports itself rather than shipping a dead handle.

    The remote half is not missing and is no longer deferred: it is
    `Session/Logs`, hand-written beside the other protocol rpcs, and
    the tests at the end of this file hold it. A log stream wants a
    server-streaming rpc rather than a handle to poll, so the
    generator stays unaware of it - which is what this gate keeps
    true."""
    from huggorm_generated._policy import ASYNC_CLASS, METHODS
    from huggorm_generated.rpc import RPCEvalState

    assert not hasattr(RPCEvalState, "subscribe_logs")
    assert "LogStream" not in METHODS, "no service, so no methods"
    # ...and no async class either. The name was in this table with
    # nothing behind it, so `server.adopt` would have raised
    # AttributeError on the first handle it tried to lease.
    assert "LogStream" not in ASYNC_CLASS
    names = [m.name for m in METHODS["EvalState"]]
    assert "subscribe_logs" not in names, names
    # ...and its inverse DOES cross, which is asserted so the
    # asymmetry reads as a decision. `unsubscribe_logs` answers
    # nothing, so the reason that refuses `subscribe_logs` - a handle
    # with no service behind it - does not apply to it.
    assert "unsubscribe_logs" in names, names


async def test_the_loop_drains_while_the_evaluator_works() -> None:
    """The destination, in one test: logs arrive OFF the eval thread.

    An `AsyncEvalState` runs its evaluation on its own affine thread.
    The queue is pool-threaded and holds its own mutex, so a drain
    runs on whatever thread asks - here the event loop's, while the
    evaluation is still in flight on another.

    That is the whole reason `LogStream` is a class rather than a
    `drain_logs()` on `EvalState`. A method there would route to the
    state's own thread and queue BEHIND the evaluation it reports on,
    so the records would arrive only once the work they describe had
    finished.

    The eval here is small, so this does not prove the drain
    OVERLAPPED it - it proves the drain does not need the eval
    thread, which is the part a wrong design would fail.

    `subscribe_logs` hands back the sync `LogStream` itself, with no
    await on its methods. That is right and is what the manifest
    says: it is pool-threaded and nothing in it can block, so a
    wrapper would buy neither a thread hop nor a released GIL."""
    from huggorm_generated import AsyncEvalState

    state = AsyncEvalState(URI)
    stream = await state.subscribe_logs()
    try:
        await state.eval_expr(TRACE % "from the loop")
        seen = " ".join(r.text() for r in stream.drain())
    finally:
        await state.unsubscribe_logs()
        await state.aclose()

    assert "from the loop" in seen


# -- the stream that crosses ----------------------------------------------
#
# Everything above holds the queue in one process. What follows holds
# the rpc over it: the one thing in this schema that travels the other
# way, unsolicited, and the only server-streaming method here.
#
# It is HAND-WRITTEN, beside Session, and `tasks/032` says why: every
# other rpc is the wire form of a declared method, and this is the wire
# form of no method at all. The generator stays unaware of it.


async def batch(stream: Any, timeout: float = 20) -> tuple[list[Any], int]:
    """The next batch, or a failure that names the wait.

    A bare `anext` would hang until pytest's own timeout and report
    nothing about which stream stopped."""
    with anyio.fail_after(timeout):
        return await anext(stream)  # type: ignore[no-any-return]


async def opened(client: Any, state: Any, **kw: Any) -> Any:
    """A stream past its empty first batch.

    That batch is the subscription saying it is installed, and waiting
    for it is what makes the next line safe: an evaluation started
    before the subscribe lands raises its records into no queue."""
    stream = client.logs(state, **kw)
    records, dropped = await batch(stream)
    assert records == [], "the first batch says 'subscribed', nothing more"
    assert dropped == 0
    return stream


async def test_a_trace_reaches_a_remote_client(client: Any) -> None:
    """The point of the task, in one test.

    Nix raises a record on the evaluation thread inside a server
    process, and a client in another process reads it while holding
    nothing but a handle."""
    state = await client.acquire("EvalState", "dummy://")
    stream = await opened(client, state)
    try:
        await state.eval_expr(TRACE % "over the wire")
        records, dropped = await batch(stream)
        assert any("over the wire" in r.text() for r in records), records
        assert dropped == 0
    finally:
        await stream.aclose()


async def test_a_record_arrives_as_a_real_local_object(client: Any) -> None:
    """A LogRecord crosses as a VALUE, so the far side gets the class.

    Not a dict and not a handle. `LogRecord` is a wire value, so the
    codec rebuilds it from its declared parts - the same round trip
    `test_store` gates for every other one - and `fields` comes back as
    a list rather than as a shape this layer invented."""
    from huggorm_bindings import LogRecord

    state = await client.acquire("EvalState", "dummy://")
    stream = await opened(client, state)
    try:
        await state.eval_expr(TRACE % "typed")
        records, _ = await batch(stream)
    finally:
        await stream.aclose()

    record = next(r for r in records if "typed" in r.text())
    assert isinstance(record, LogRecord)
    assert record.action() == "msg"
    assert isinstance(record.fields(), list)


async def test_the_level_narrows_over_the_wire_too(client: Any) -> None:
    """Level 0 is a subscription, not an absence.

    lvlError is 0, so "errors only" and "no level given" are different
    requests that a plain proto3 sint64 cannot tell apart. The field
    has real presence for exactly this, and this is the gate on it: a
    zero that arrived as unset would take the default of 3 and let the
    warning through."""
    state = await client.acquire("EvalState", "dummy://")
    stream = await opened(client, state, level=0)
    try:
        await state.eval_expr(TRACE % "kept")
        await state.eval_expr(WARN % "refused")
        records, _ = await batch(stream)
        seen = " ".join(r.text() for r in records)
    finally:
        await stream.aclose()

    assert "kept" in seen
    assert "refused" not in seen


async def test_a_drop_crosses_rather_than_vanishing(client: Any) -> None:
    """The bound is remote too, and it still says when it was reached.

    A client that cannot tell a quiet evaluation from a lost one is
    looking at this repo's named failure mode over a socket instead of
    in a process. `dropped` rides with every batch, cumulative, so it
    survives a batch the reader skipped.

    ONE expression, and that is the whole reason this reads the way it
    does. The server drains every 50ms while the test runs, so five
    sequential `eval_expr` round trips race the poll: a tick landing
    between two of them empties a capacity-1 queue, the next record is
    kept rather than refused, and `dropped` comes back short. Nested
    traces push five records microseconds apart, so no tick fits
    between them and the count is exact rather than likely.

    The OUTERMOST trace prints first, so "the earliest survives" is
    still what the bound says."""
    nest = "1"
    for i in reversed(range(5)):
        nest = f'builtins.trace "line{i}" ({nest})'

    state = await client.acquire("EvalState", "dummy://")
    stream = await opened(client, state, capacity=1)
    try:
        await state.eval_expr(nest)
        records, dropped = await batch(stream)
    finally:
        await stream.aclose()

    assert len(records) == 1, "the bound held over the wire"
    assert "line0" in records[0].text(), records
    assert dropped == 4, records


async def test_one_reader_per_state(client: Any) -> None:
    """A second stream on one state is REFUSED, not accepted.

    The tap routes by thread and a second subscribe replaces the first
    in the C++, so accepting this would leave the older stream open,
    connected, and empty forever - which reads exactly like a state
    that stopped logging.

    FAILED_PRECONDITION and not an InternalError: the guard around the
    Session handlers wraps everything it catches, and a deliberate
    refusal wrapped that way reads as a bug in the server."""
    from grpclib.const import Status
    from grpclib.exceptions import GRPCError

    state = await client.acquire("EvalState", "dummy://")
    first = await opened(client, state)
    try:
        second = client.logs(state)
        with pytest.raises(GRPCError) as caught:
            await batch(second)
        assert caught.value.status is Status.FAILED_PRECONDITION
        assert "already has a log stream" in (caught.value.message or "")
    finally:
        await first.aclose()


async def test_a_closed_stream_gives_the_state_back(client: Any) -> None:
    """Closing one reader lets the next one in.

    This is the gate on the registry entry being RELEASED. Drop the
    `pop` from the handler's `finally` and this is what fails, with a
    TimeoutError rather than a hang, because the retry below turns a
    permanent refusal into one.

    It does NOT gate the ORDER of that cleanup. The handler puts the
    synchronous work first on the argument that a cancelled task
    cannot await, and two perturbations refute the argument: awaiting
    before the pop passes, and an `asyncio.sleep` before it passes.
    Recorded in `tasks/032`, because a defence nothing tests is worth
    saying out loud."""
    state = await client.acquire("EvalState", "dummy://")
    first = await opened(client, state)
    await first.aclose()

    # The server learns of the close through a stream reset, so the
    # `finally` runs a moment after aclose() returns here. Retrying is
    # the honest wait: a fixed sleep would be a guess about a machine.
    with anyio.fail_after(20):
        while True:
            try:
                second = await opened(client, state)
            except Exception:
                await anyio.sleep(0.05)
            else:
                break
    await second.aclose()


async def test_a_swept_connection_ends_the_stream(ttl_server: Server) -> None:
    """A stream that lost its connection SAYS so.

    Transports never report death, so the sweeper is what notices a
    silent connection and releases what it held (tasks/002). A log
    stream outlives one poll of that, and a stream that simply stopped
    would be indistinguishable from a quiet evaluation - this repo's
    named failure mode, spelled as an absence of messages.

    So the handler ends it with UNAVAILABLE, and the check is a READ:
    `table.entries`, not `table.alive`. `alive` REFRESHES the
    connection it is asked about, so a log stream asking it every 50ms
    would keep its own connection alive forever and quietly disable
    the sweeper for it. Liveness stays the ping loop's job, which is
    why this test stops the pinging to get a sweep at all.

    It uses the short-TTL server, so it costs one sweep of wall time
    rather than the default lease."""
    from grpclib.const import Status
    from grpclib.exceptions import GRPCError

    from huggorm import remote

    c = await remote.connect(HOST, ttl_server.port)
    state = await c.acquire("EvalState", "dummy://")
    stream = await opened(c, state)
    # Nothing keeps the connection alive now, so the next sweep takes
    # it - and the handle with it.
    c.stop_pinging()
    with pytest.raises(GRPCError) as caught:
        await batch(stream, timeout=SHORT_TTL * 4 + 10)
    assert caught.value.status is Status.UNAVAILABLE
    assert "swept" in (caught.value.message or "")


async def test_the_descriptor_says_it_streams() -> None:
    """The schema has to SAY server-streaming, not just behave it.

    Reflection and grpcurl read this flag, so a descriptor that calls
    Logs unary while dispatch streams is a schema that lies - and a
    client built from it would wait for one message and stop.

    No other Session method carries it, which is asserted rather than
    assumed: this is the first use of the flag in this schema, so
    there was no example to copy and nothing to notice a stray one."""
    from huggorm.grpc_pb import PKG, load_pool

    session = load_pool().FindServiceByName(  # type: ignore[no-untyped-call]
        f"{PKG}.Session")
    streaming = {m.name for m in session.methods if m.server_streaming}
    assert streaming == {"Logs"}, streaming
    logs = session.FindMethodByName(  # type: ignore[no-untyped-call]
        "Logs")
    assert not logs.client_streaming, "the request is one message"
