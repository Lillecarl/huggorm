"""Scopes onto Nix: local and remote sessions.

A session hands out stores and evaluators, refuses a store from
another session, and closes what it made. `dummy://` is in-memory
and the server is a localhost subprocess, so every test here is
hermetic.
"""

import inspect
from collections.abc import Callable
from typing import Any

import anyio
import pytest
from conftest import SHORT_TTL

from huggorm.remote import ConnectionExpired
from huggorm.session import (
    AsyncRemoteSession,
    AsyncRemoteSessionLike,
    AsyncSession,
    AsyncSessionLike,
)
from huggorm_generated import AsyncEvalState, AsyncStore
from huggorm_generated._policy import ACQUIRE
from huggorm_generated._runtime import InternalError


def _connect(server: Any, claim: str | None = None) -> Any:
    """One remote session on this test's server, over `dummy://`."""
    return AsyncRemoteSession.connect("127.0.0.1", server.port, claim=claim, store_uri="dummy://")


# builtins.trace goes through printError, which is lvlError - 0, and
# under every verbosity there is. So it is the one message a test can
# rely on arriving.
TRACE = 'builtins.trace "%s" 1'


async def test_evaluates_through_session_stores() -> None:
    async with AsyncSession("dummy://") as session:
        store = session.store()
        state = session.eval(store)
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2
        assert isinstance(session, AsyncSessionLike)


async def test_explicit_uri_wins_over_the_default(tmp_path: Any) -> None:
    async with AsyncSession("dummy://") as session:
        explicit = await session.store(str(tmp_path)).get_uri()
        default = await session.store().get_uri()
        assert default.startswith("dummy")
        assert explicit.startswith("local")


async def test_foreign_store_is_refused() -> None:
    async with AsyncSession("dummy://") as first, AsyncSession("dummy://") as second:
        with pytest.raises(ValueError, match="another session"):
            second.eval(first.store())


async def test_close_is_idempotent() -> None:
    session = AsyncSession("dummy://")
    state = session.eval(session.store())
    await state.eval_expr("1 + 1")
    calls = 0
    close = state.aclose

    async def counted() -> None:
        nonlocal calls
        calls += 1
        await close()

    state.aclose = counted  # type: ignore[method-assign]
    await session.aclose()
    await session.aclose()
    assert calls == 1


def _params(fn: Callable[..., Any], drop: int) -> list[tuple[str, object]]:
    return [
        (name, param.default)
        for name, param in list(inspect.signature(fn).parameters.items())[drop:]
    ]


def test_session_passes_constructors_through() -> None:
    """The session adds scope, not parameters.

    `eval()` takes exactly what the generated constructor takes, so
    a declaration change that moves a signature fails here rather
    than drifting silently. `store()` names the same parameter, but
    its default is `None` (the session default) where the bare
    constructor says `'auto'`; that difference is the session, so
    the gate names it instead of forbidding it.
    """
    session_names = [n for n, _ in _params(AsyncSession.store, 1)]
    generated_names = [n for n, _ in _params(AsyncStore.__init__, 1)]
    assert session_names == generated_names
    assert _params(AsyncSession.eval, 2) == _params(AsyncEvalState.__init__, 2)
    assert _params(AsyncSessionLike.eval, 2) == _params(AsyncEvalState.__init__, 2)


async def test_remote_evaluates_through_session(server: Any) -> None:
    async with _connect(server) as session:
        store = await session.store()
        value = await (await session.eval(store)).eval_expr("1 + 1")
        assert await value.integer() == 2
        assert isinstance(session, AsyncRemoteSessionLike)


async def test_remote_foreign_store_is_refused(server: Any) -> None:
    first_ctx = _connect(server)
    second_ctx = _connect(server)
    async with first_ctx as first, second_ctx as second:
        with pytest.raises(ValueError, match="another session"):
            await second.eval(await first.store())


async def test_remote_close_is_idempotent(server: Any) -> None:
    ctx = _connect(server)
    async with ctx as session:
        state = await session.eval(await session.store())
        await state.eval_expr("1 + 1")
        calls = 0
        release = session._client.release

        async def counted(obj: Any) -> None:
            nonlocal calls
            calls += 1
            await release(obj)

        session._client.release = counted  # type: ignore[method-assign]
        await session.aclose()
        await session.aclose()
        assert calls == 2


async def test_remote_detach_keeps_token(server: Any) -> None:
    ctx = _connect(server)
    async with ctx as session:
        await session.store()
        token = session.token
        assert token
        assert await session.detach(all=True) is True
        assert session.token == token


async def test_remote_swept_connection_reports(ttl_server: Any) -> None:
    """A swept session reports, through the same path the client does.

    The ping loop learns of the sweep and the next call raises
    `ConnectionExpired`. Closing afterwards reports nothing: the
    releases fail against a connection that is already gone.
    """
    ctx = _connect(ttl_server)
    async with ctx as session:
        store = await session.store()
        session._client.stop_pinging()
        await anyio.sleep(SHORT_TTL * 1.5 + 1.0)
        with anyio.fail_after(5):
            await session._client._ping_loop(0.01)
        with pytest.raises(ConnectionExpired):
            await store.get_uri()
        await session.aclose()


async def test_remote_eval_takes_a_build_store_of_its_own(
        server: Any) -> None:
    """`build_store` is a handle on the server, as `store` is."""
    ctx = _connect(server)
    async with ctx as session:
        store = await session.store()
        state = await session.eval(store, build_store=await session.store())
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2


async def test_remote_close_reports_a_failed_release(server: Any) -> None:
    """The counterpart: on a live connection, a failed release raises.

    The handle id is one the server never issued, so its release
    fails while the connection still answers pings."""
    ctx = _connect(server)
    async with ctx as session:
        store = await session.store()
        store.handle_id = "0" * 32
        with pytest.raises(InternalError, match="release") as caught:
            await session.aclose()
        assert "lease(s) on 00000000" in str(caught.value.__cause__)


def test_remote_session_follows_the_acquire_table() -> None:
    """Typed acquires name what the declaration says.

    The acquire table is emitted from the model, so a declaration change
    that moves a constructor fails here rather than drifting the
    hand-written session silently.
    """
    store_names = [n for n, _ in _params(AsyncRemoteSession.store, 1)]
    assert store_names == [a.name for a in ACQUIRE["Store"].args]
    eval_names = [n for n, _ in _params(AsyncRemoteSession.eval, 1)]
    assert eval_names == [a.name for a in ACQUIRE["EvalState"].args]


async def test_warm_state_survives_its_client(
        server: Any, tmp_path: Any) -> None:
    """The shareable-server property: a client leaves, the state stays.

    The first session evaluates a file, detaches everything into
    escrow, and closes. The file then changes on disk. The second
    session claims the token and attaches the evaluator by its id:
    `fileEvalCache` still holds the first answer, so only the SAME
    warm state gives it. A fresh state reads the new text, which is
    the control that shows the file did change. This is the shape
    nanopynix cannot serve: a worker bound to one session's lifetime.
    """
    source = tmp_path / "warm.nix"
    source.write_text("40 + 2")
    first_ctx = _connect(server)
    async with first_ctx as first:
        state = await first.eval(await first.store())
        value = await state.eval_file(str(source))
        assert await value.integer() == 42
        token = first.token
        state_id = state.handle_id
        assert token and state_id
        assert await first.detach(all=True) is True
    source.write_text("0")
    second_ctx = _connect(server, claim=token)
    async with second_ctx as second:
        adopted = second.attach("EvalState", state_id)
        value = await adopted.eval_file(str(source))
        assert await value.integer() == 42
        cold = await second.eval(await second.store())
        value = await cold.eval_file(str(source))
        assert await value.integer() == 0


async def test_local_capture_collects_evaluation_logs() -> None:
    """Collecting what a block raised, locally.

    Subscribing installs synchronously, so no gate is needed before
    the work: everything the block raises is already queued when it
    ends, and the last drain is complete.
    """
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        async with session.capture(state) as caught:
            await state.eval_expr(TRACE % "hello-local")
    assert any("trace: hello-local" in r.text() for r in caught.records)
    assert caught.dropped == 0


async def test_local_logs_streams_while_work_runs() -> None:
    """The live stream, locally.

    The empty first batch says the subscription is installed, as on
    the remote stream, so the eval after it cannot run ahead of the
    queue.
    """
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        it = session.logs(state)
        installed, _ = await anext(it)
        assert installed == []
        await state.eval_expr(TRACE % "hello-stream")
        with anyio.fail_after(10):
            async for records, _ in it:
                if any("trace: hello-stream" in r.text() for r in records):
                    break
        await it.aclose()


async def test_a_nested_capture_silences_no_outer_reader() -> None:
    """Two local readers on one state share one subscription.

    The binding REPLACES a thread's subscription, so a `capture()`
    that subscribed on its own left the outer `logs()` loop connected
    and silent, forever (huggorm#85). Both join the state's tap now:
    the capture sees its record, and the outer loop hears that one
    and the one raised after the capture ended."""
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        seen: list[str] = []
        outer = session.logs(state, poll=0.01)
        assert await anext(outer) == ([], 0)

        async def read_until(text: str) -> None:
            with anyio.fail_after(10):
                async for records, _ in outer:
                    seen.extend(r.text() for r in records)
                    if any(text in s for s in seen):
                        return

        async with anyio.create_task_group() as tg:
            tg.start_soon(read_until, "INNER")
            async with session.capture(state) as captured:
                await state.eval_expr(TRACE % "INNER")
        await state.eval_expr(TRACE % "AFTER")
        await read_until("AFTER")
        await outer.aclose()

    assert any("INNER" in r.text() for r in captured.records)
    assert any("INNER" in s for s in seen), seen
    assert any("AFTER" in s for s in seen), seen


async def test_a_cancelled_reader_still_unsubscribes() -> None:
    """Cancelling the consumer is the usual way out of `logs`.

    anyio re-delivers a cancel at each await in the generator's
    `finally`, so an unshielded teardown stops at its first await and
    never unsubscribes - which leaks the thread's verbosity
    (huggorm#95). Unshield it and `unsubscribed` stays empty."""
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        unsubscribed: list[bool] = []
        real = state.unsubscribe_logs

        async def spy() -> None:
            await real()
            unsubscribed.append(True)

        state.unsubscribe_logs = spy  # type: ignore[method-assign]
        with anyio.move_on_after(0.2):
            async for _ in session.logs(state):
                pass
    assert unsubscribed


async def test_remote_logs_stream_reports(server: Any) -> None:
    """The live stream, remotely.

    The empty first batch is the gate the local stream lacks: it
    says the subscription is installed, so the eval after it cannot
    run ahead of the queue.
    """
    ctx = _connect(server)
    async with ctx as session:
        state = await session.eval(await session.store())
        it = session.logs(state).__aiter__()
        installed, _ = await anext(it)
        assert installed == []
        await state.eval_expr(TRACE % "hello-remote")
        with anyio.fail_after(10):
            async for records, _ in it:
                if any("trace: hello-remote" in r.text() for r in records):
                    break
        await it.aclose()


async def test_remote_capture_collects(server: Any) -> None:
    """Collecting what a block raised, remotely, completely.

    Records cross a socket, so the block ending is not the last one
    landing. The capture reads to the barrier's marker, not for a
    window: with `settle=0` a window reads nothing at all, and this
    still holds every record. The marker itself is not a record of
    the block.
    """
    ctx = _connect(server)
    async with ctx as session:
        state = await session.eval(await session.store())
        async with session.capture(state, settle=0) as caught:
            await state.eval_expr(TRACE % "hello-captured")
    assert any("trace: hello-captured" in r.text() for r in caught.records)
    assert caught.dropped == 0
    finals = {r.request() for r in caught.records if r.action() == "finalized"}
    assert len(finals) == 1, finals


async def test_a_remote_warning_crosses_with_its_error_info(
        server: Any) -> None:
    """A record raised by `logEI` crosses with its parts: the same
    `ErrorInfo` a failed call's error carries (huggorm#85, gap 4)."""
    ctx = _connect(server)
    async with ctx as session:
        state = await session.eval(await session.store())
        async with session.capture(state) as caught:
            await state.eval_expr('builtins.warn "careful" 1')
    warned = [r for r in caught.records if r.action() == "msg"]
    assert warned, caught.records
    info = warned[0].info()
    assert info is not None, "the parts did not cross"
    assert info.msg() == "careful"


async def test_remote_process_logs_opens(server: Any) -> None:
    """The process stream opens, with the same installed signal."""
    ctx = _connect(server)
    async with ctx as session:
        it = session.process_logs().__aiter__()
        installed, _ = await anext(it)
        assert installed == []
        await it.aclose()


async def test_local_capture_collects_bytes_it_cannot_decode(
        tmp_path: Any) -> None:
    """A capture holds the bytes, not the decoding of them."""
    blob = tmp_path / "blob"
    blob.write_bytes(b"\xff\xfe")
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        async with session.capture(state) as caught:
            await state.eval_expr(
                f'builtins.trace (builtins.readFile "{blob}") 1')
    assert b"trace: \xff\xfe" in [r.text_bytes() for r in caught.records
                                  if r.action() == "msg"]


async def test_remote_capture_collects_bytes_it_cannot_decode(
        server: Any, tmp_path: Any) -> None:
    """The same, across the socket: the bytes cross as bytes."""
    blob = tmp_path / "blob"
    blob.write_bytes(b"\xff\xfe")
    ctx = _connect(server)
    async with ctx as session:
        state = await session.eval(await session.store())
        async with session.capture(state) as caught:
            await state.eval_expr(
                f'builtins.trace (builtins.readFile "{blob}") 1')
    assert b"trace: \xff\xfe" in [r.text_bytes() for r in caught.records
                                  if r.action() == "msg"]
