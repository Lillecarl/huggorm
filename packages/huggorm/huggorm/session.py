"""Two scopes onto Nix, and the objects each one made.

A session owns a default store URI and every store and evaluator
handed out from it. Two sessions with different URIs coexist in one
process. An evaluator only accepts a store from its own session.

`AsyncSession` is local: wrappers build lazily, so `store()` and
`eval()` are plain methods. `AsyncRemoteSession` builds over RPC, so
its two are coroutines. That asymmetry is honest rather than agreed:
making the local pair async would pretend to do work, and a remote
build cannot be sync. Each flavour has its own protocol for that
reason; the objects they hand out already share one.

Both scopes stream what Nix says while it works. `logs()` passes the
records through as they arrive; `capture()` collects them for the
length of a block.
"""

from __future__ import annotations

import contextlib
import logging
import weakref
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload, runtime_checkable

import anyio

from huggorm_generated import AsyncEvalState, AsyncStore, RPCEvalState, RPCStore, collect_garbage

from .logbus import LOG_CAPACITY, LOG_LEVEL, Share, widest
from .remote import NixClient, connect

if TYPE_CHECKING:
    from huggorm_bindings import LogRecord, Store

logger = logging.getLogger(__name__)

LogBatch = tuple[list["LogRecord"], int]
"""One drain: the records waiting, and how many the bound refused.

`dropped` is cumulative over the subscription, so a reader that
missed a batch still sees the number grow. A reader wanting the
per-batch figure subtracts.
"""


@dataclass
class CapturedLogs:
    """What Nix said while a `capture()` block ran.

    `records` in arrival order; `dropped` the highest count any drain
    reported, so a quiet capture and a lossy one read differently.
    """

    records: list[LogRecord]
    dropped: int = 0


class _Tap:
    """One subscription on one state, shared by every local reader.

    PULL-based, where the server's `_Fanout` pushes: whichever reader
    drains reads the shared queue and offers every record to every
    reader. So no task drains in the background, and a session needs
    no task group - it still works without `async with`.

    One per state, process-wide, and never removed while the state
    lives. Removing an empty tap would let a `join` that already holds
    it open a subscription nobody can find, and the next reader would
    open a second one, which the binding answers by replacing the
    first (huggorm#85).
    """

    def __init__(self, state: AsyncEvalState) -> None:
        # Weak: `_TAPS` is keyed weakly by the state, and a strong
        # reference here would keep the key alive forever.
        self._state = weakref.ref(state)
        self._stream: Any = None
        self._level: int | None = None
        self._readers: set[_LocalReader] = set()
        self._lock = anyio.Lock()
        self.dropped = 0

    async def join(self, capacity: int, level: int) -> _LocalReader:
        reader = _LocalReader(self, capacity, level)
        async with self._lock:
            if self._stream is not None and level > (self._level or 0):
                # Wider than the live subscription: hand out what it
                # holds, then reopen at the new widest level.
                await self._pull()
                await self._close()
            if self._stream is None:
                state = self._state()
                assert state is not None, "a joining caller holds the state"
                self._level = max(level, widest(self._readers))
                self._stream = await state.subscribe_logs(
                    LOG_CAPACITY, self._level)
                self.dropped = 0
                # The subscribe is a call, and its own "finalized"
                # lands in the queue it just installed. No reader asked
                # for that call, so it goes before any reader joins.
                await self._stream.drain()
            self._readers.add(reader)
        return reader

    async def pull(self) -> None:
        async with self._lock:
            await self._pull()

    async def _pull(self) -> None:
        """LOCK HELD. Offer what the shared queue holds to every reader."""
        if self._stream is None:
            return
        records = await self._stream.drain()
        self.dropped = await self._stream.dropped()
        for record in records:
            for reader in self._readers:
                reader.offer(record)

    async def leave(self, reader: _LocalReader) -> None:
        async with self._lock:
            self._readers.discard(reader)
            if not self._readers and self._stream is not None:
                await self._close()

    async def _close(self) -> None:
        """LOCK HELD. Drop the shared subscription."""
        stream, self._stream, self._level = self._stream, None, None
        await stream.close()
        if (state := self._state()) is not None:
            await state.unsubscribe_logs()


class _LocalReader(Share):
    """One local reader's share of a state's tap."""

    def __init__(self, tap: _Tap, capacity: int, level: int) -> None:
        super().__init__(capacity, level)
        self._tap = tap

    async def drain(self) -> list[LogRecord]:
        await self._tap.pull()
        return self.take()

    def dropped(self) -> int:
        """This reader's drops plus the shared queue's. Cumulative."""
        return self.own_dropped + self._tap.dropped

    async def leave(self) -> None:
        await self._tap.leave(self)


_TAPS: weakref.WeakKeyDictionary[AsyncEvalState, _Tap] = (
    weakref.WeakKeyDictionary())


def _tap(state: AsyncEvalState) -> _Tap:
    tap = _TAPS.get(state)
    if tap is None:
        tap = _TAPS[state] = _Tap(state)
    return tap


@runtime_checkable
class AsyncSessionLike(Protocol):
    """What every local async session promises."""

    __slots__ = ()

    def store(self, uri: str | None = None) -> AsyncStore:
        """A store on this session, not yet opened."""
        ...

    def eval(
        self,
        store: AsyncStore,
        settings: dict[str, str] | None = None,
        build_store: Store | None = None,
    ) -> AsyncEvalState:
        """An evaluator bound to `store`, not yet opened."""
        ...

    async def aclose(self) -> None:
        """Release every object this session handed out. Idempotent."""
        ...

    def logs(
        self,
        state: AsyncEvalState,
        capacity: int = LOG_CAPACITY,
        level: int = LOG_LEVEL,
        poll: float = 0.05,
    ) -> AsyncGenerator[LogBatch]:
        """Records raised on this state's thread, as they arrive."""
        ...

    def capture(
        self,
        state: AsyncEvalState,
        capacity: int = LOG_CAPACITY,
        level: int = LOG_LEVEL,
    ) -> contextlib.AbstractAsyncContextManager[CapturedLogs]:
        """Collect what this state says while the block runs."""
        ...


class AsyncSession:
    """A scope for local async Nix objects.

    `store()` and `eval()` build wrappers lazily: nothing runs until
    the first call. `aclose()` shuts the evaluators down, each on its
    own thread, then the stores. Stores share the process pool, so
    closing one releases nothing but the wrapper.
    """

    def __init__(self, store_uri: str = "auto") -> None:
        self._store_uri = store_uri
        self._stores: set[AsyncStore] = set()
        self._evals: set[AsyncEvalState] = set()
        self._closed = False

    def store(self, uri: str | None = None) -> AsyncStore:
        """Make a store, tracked by this session.

        `None` means the session default. The wrapper builds lazily,
        so this never touches Nix.
        """
        store = AsyncStore(uri if uri is not None else self._store_uri)
        self._stores.add(store)
        return store

    def eval(
        self,
        store: AsyncStore,
        settings: dict[str, str] | None = None,
        build_store: Store | None = None,
    ) -> AsyncEvalState:
        """Make an evaluator on a store this session made.

        A foreign store raises `ValueError`. An evaluator reads its
        store's configuration at build time, so one from another
        session would silently keep the wrong one.
        """
        if store not in self._stores:
            raise ValueError(
                "this store belongs to another session; "
                "make the evaluator where the store was made"
            )
        state = AsyncEvalState(store, settings, build_store)
        self._evals.add(state)
        return state

    async def logs(
        self,
        state: AsyncEvalState,
        capacity: int = LOG_CAPACITY,
        level: int = LOG_LEVEL,
        poll: float = 0.05,
    ) -> AsyncGenerator[LogBatch]:
        """Records raised on this state's thread, as they arrive.

        Polls the subscription's queue: a drain never blocks, so
        there is nothing to wait on, only a queue to re-read.
        `capacity` and `level` mean what `subscribe_logs` says.
        The FIRST batch is empty and says the subscription is
        installed, as on the remote stream: start the work after
        reading it. After that, empty polls yield nothing.

        Any state's records read here, not only one this session
        made. Watching creates no lease, so there is nothing to own.
        Every reader on one state shares one subscription, so a
        `capture()` inside this loop silences nothing. Stopping the
        iteration leaves it, and the last reader out ends it.
        """
        reader = await _tap(state).join(capacity, level)
        try:
            yield ([], reader.dropped())
            while True:
                records = await reader.drain()
                if records:
                    yield (records, reader.dropped())
                await anyio.sleep(poll)
        finally:
            # Shielded: a cancelled consumer is the usual way out of
            # this loop, and anyio re-delivers the cancel at each await
            # here, which would skip the unsubscribe and leak the
            # thread's verbosity (huggorm#95).
            with anyio.CancelScope(shield=True):
                await reader.leave()

    @contextlib.asynccontextmanager
    async def forward(
        self,
        state: AsyncEvalState,
        callback: Callable[[LogRecord], None],
        capacity: int = LOG_CAPACITY,
        level: int = LOG_LEVEL,
        poll: float = 0.05,
    ) -> AsyncIterator[None]:
        """Hand each record this state raises to `callback`, while the
        block runs.

        Live, as `logs` is, and complete, as `capture` is: when the
        block ends, one last drain hands over what is still queued. A
        consumer of `logs` cannot have that tail, because a cancelled
        generator yields nothing more, and the record lost is the one
        raised last - often the warning a caller most needs.

        `callback` runs on the event loop, between polls, and must not
        block. An error in the block reaches the caller as itself, not
        wrapped in the task group's `ExceptionGroup`.
        """
        reader = await _tap(state).join(capacity, level)

        async def pump() -> None:
            while True:
                for record in await reader.drain():
                    callback(record)
                await anyio.sleep(poll)

        failure: Exception | None = None
        try:
            async with anyio.create_task_group() as group:
                group.start_soon(pump)
                try:
                    yield
                except Exception as e:
                    failure = e
                finally:
                    group.cancel_scope.cancel()
        finally:
            # Shielded, as in `logs`: a cancelled block still hands
            # over its tail and still unsubscribes.
            with anyio.CancelScope(shield=True):
                for record in await reader.drain():
                    callback(record)
                await reader.leave()
        if failure is not None:
            raise failure

    @contextlib.asynccontextmanager
    async def capture(
        self,
        state: AsyncEvalState,
        capacity: int = LOG_CAPACITY,
        level: int = LOG_LEVEL,
    ) -> AsyncIterator[CapturedLogs]:
        """Collect what this state says while the block runs.

        Subscribing installs synchronously, so everything the block
        raises is already queued when it ends, and one last drain
        collects it: complete, not eventual. The `finally` drains on
        the way out however the block ends, and leaves the shared
        subscription, as `logs` does.
        """
        reader = await _tap(state).join(capacity, level)
        out = CapturedLogs(records=[])
        try:
            yield out
        finally:
            # Shielded, as in `logs`: a cancelled block still drains
            # and still unsubscribes.
            with anyio.CancelScope(shield=True):
                out.records.extend(await reader.drain())
                out.dropped = max(out.dropped, reader.dropped())
                await reader.leave()

    async def aclose(self) -> None:
        """Close the evaluators, then the stores, and report together.

        Evaluators go first: a store cannot shut down while an
        evaluator still reads it. Each failure is collected, so the
        first one does not strand the rest. One failure raises alone;
        several raise as an `ExceptionGroup`. A closed session stays
        closed.
        """
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        closed_any = bool(self._evals)
        for state in tuple(self._evals):
            try:
                await state.aclose()
            except Exception as exc:
                errors.append(exc)
        self._evals.clear()
        if closed_any:
            # A closed evaluator's Store is not free yet. A caught
            # evaluation error lives in a finalizable Boehm block whose
            # position holds rootFS, and rootFS mounts the Store, so the
            # Store and its daemon connections outlive the evaluator
            # until a collection finalizes that block - in a small
            # process, possibly never (huggorm#128). nixpkgs raises one
            # on every `import nixpkgs { }`. Measured: 10 ms after a
            # nixpkgs evaluation.
            await collect_garbage()
        for store in tuple(self._stores):
            try:
                await store.aclose()
            except Exception as exc:
                errors.append(exc)
        self._stores.clear()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("closing the session failed", errors)

    async def __aenter__(self) -> AsyncSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()


@runtime_checkable
class AsyncRemoteSessionLike(Protocol):
    """What every remote async session promises.

    `detach`, `share` and `token` stay off this protocol: they are
    the lease mechanics of one connection, and a protocol promising
    them would make every future flavour carry leases too.
    """

    __slots__ = ()

    async def store(self, uri: str | None = None) -> RPCStore:
        """A store on the server, built now."""
        ...

    async def eval(
        self,
        store: RPCStore,
        settings: dict[str, str] | None = None,
        build_store: RPCStore | None = None,
    ) -> RPCEvalState:
        """An evaluator on the server, bound to `store`."""
        ...

    async def aclose(self) -> None:
        """Release every handle this session holds. Idempotent."""
        ...

    def logs(
        self,
        state: RPCEvalState,
        capacity: int = 0,
        level: int | None = None,
    ) -> AsyncGenerator[LogBatch]:
        """Records raised on this state's server thread, as they arrive."""
        ...

    def process_logs(
        self,
        capacity: int = 0,
        level: int | None = None,
    ) -> AsyncGenerator[LogBatch]:
        """Records no subscribed thread claimed, as they arrive."""
        ...

    def capture(
        self,
        state: RPCEvalState | None = None,
        capacity: int = 0,
        level: int | None = None,
        settle: float = 1.0,
    ) -> contextlib.AbstractAsyncContextManager[CapturedLogs]:
        """Collect what the server says while the block runs."""
        ...


class AsyncRemoteSession:
    """A scope onto one connection, and the handles it holds.

    Takes an open, bound client: entering and binding stays with
    `remote.connect`, so this class never owns the ping loop. It
    holds every object it hands out strongly, so no finalizer frees
    a handle behind it; `aclose` releases evaluators before stores,
    as locally. A handle released twice is skipped, not reported:
    the caller already did that work.

    A swept connection is reported, never repaired. The next call
    raises `ConnectionExpired`, and re-binding is the caller's call:
    a fresh bind would hand back live-looking objects whose handles
    are gone.
    """

    def __init__(self, client: NixClient, store_uri: str = "auto") -> None:
        if client.token is None:
            raise ValueError("the client is not bound; `await client.bind()` first")
        self._client = client
        self._store_uri = store_uri
        self._stores: set[RPCStore] = set()
        self._evals: set[RPCEvalState] = set()
        self._closed = False

    @classmethod
    @contextlib.asynccontextmanager
    async def connect(
        cls,
        host: str = "127.0.0.1",
        port: int = 50051,
        claim: str | None = None,
        store_uri: str = "auto",
    ) -> AsyncIterator[AsyncRemoteSession]:
        """Connect, bind (claiming escrow with `claim`), and scope one session.

        `claim` is a token a previous session detached under: the
        server adopts its escrowed leases onto this connection.
        """
        async with connect(host, port, claim=claim) as client:
            session = cls(client, store_uri)
            try:
                yield session
            finally:
                await session.aclose()

    @property
    def token(self) -> str | None:
        """This connection's identity. Detached leases rest under it."""
        return self._client.token

    async def store(self, uri: str | None = None) -> RPCStore:
        """Build a store on the server, tracked by this session."""
        chosen = uri if uri is not None else self._store_uri
        proxy: RPCStore = await self._client.acquire("Store", chosen)
        self._stores.add(proxy)
        return proxy

    async def eval(
        self,
        store: RPCStore,
        settings: dict[str, str] | None = None,
        build_store: RPCStore | None = None,
    ) -> RPCEvalState:
        """Build an evaluator on a store this session made.

        A foreign store raises `ValueError`: the evaluator would pin
        a store this connection tracks elsewhere, and releasing it
        here would pull it out from under its owner.
        """
        if store not in self._stores:
            raise ValueError(
                "this store belongs to another session; "
                "make the evaluator where the store was made"
            )
        proxy: RPCEvalState = await self._client.acquire("EvalState", store, settings, build_store)
        self._evals.add(proxy)
        return proxy

    async def detach(self, obj: Any = None, all: bool = False) -> bool:
        """Move leases into escrow under this connection's token.

        The objects outlive this session: a later `connect` with
        `claim=self.token` adopts them. What is detached is untracked
        here, so `aclose` does not end what detaching saved.
        """
        done = await self._client.detach(obj, all=all)
        if done:
            if all:
                self._evals.clear()
                self._stores.clear()
            elif obj is not None:
                self._evals.discard(obj)
                self._stores.discard(obj)
        return done

    async def share(self, obj: Any, to_token: str, mode: str = "copy") -> None:
        """Give another live connection a lease on `obj`."""
        await self._client.share(obj, to_token, mode)

    @overload
    def attach(self, cls_name: Literal["Store"], handle_id: str) -> RPCStore: ...

    @overload
    def attach(self, cls_name: Literal["EvalState"], handle_id: str) -> RPCEvalState: ...

    def attach(self, cls_name: str, handle_id: str) -> RPCStore | RPCEvalState:
        """Adopt a live handle onto this connection, tracked here.

        The other half of `share` and of detach-then-`claim`: a handle
        id passed out of band, or noted before its session closed,
        becomes a usable object here. Naming it makes this connection
        a holder, so the object stays alive. Only session-level
        classes (`Store`, `EvalState`) attach here; anything else goes
        through `client.proxy` directly.
        """
        if cls_name not in ("Store", "EvalState"):
            raise ValueError(f"only Store and EvalState attach to a session, not {cls_name!r}")
        if cls_name == "Store":
            store: RPCStore = self._client.proxy(cls_name, handle_id)
            self._stores.add(store)
            return store
        state: RPCEvalState = self._client.proxy(cls_name, handle_id)
        self._evals.add(state)
        return state

    async def _release(self, obj: Any) -> None:
        if obj.handle_id is None:
            return
        await self._client.release(obj)

    async def logs(
        self,
        state: RPCEvalState,
        capacity: int = 0,
        level: int | None = None,
    ) -> AsyncGenerator[LogBatch]:
        """Records raised on this state's server thread, as they arrive.

        Passes `client.logs` through unchanged: batches, `dropped`
        and the empty first batch mean what they mean there. That
        first batch is empty, and it says the subscription is
        installed - start the work after reading it.
        """
        async for batch in self._client.logs(state, capacity, level):
            yield batch

    async def process_logs(
        self,
        capacity: int = 0,
        level: int | None = None,
    ) -> AsyncGenerator[LogBatch]:
        """Records no subscribed thread claimed, as they arrive.

        Passes `client.process_logs` through: fetcher threads, file
        transfers and builds. Read both streams to see everything,
        and neither repeats the other.
        """
        async for batch in self._client.process_logs(capacity, level):
            yield batch

    @contextlib.asynccontextmanager
    async def capture(
        self,
        state: RPCEvalState | None = None,
        capacity: int = 0,
        level: int | None = None,
        settle: float = 1.0,
    ) -> AsyncIterator[CapturedLogs]:
        """Collect what the server says while the block runs.

        Opens the stream and reads the empty first batch, so the
        subscription is installed before the block starts.

        A state's capture is COMPLETE. When the block ends, a barrier
        call on the state's thread answers a request id, and the
        capture reads until that request's "finalized" record, which
        the thread queued after everything it raised before. `settle`
        plays no part.

        `state=None` captures the process stream, which has no such
        order: its records come from threads no call owns. It reads
        for `settle` seconds after the block, and a record later than
        that is not in `records`.

        Nothing reads during the block. The server holds the records
        meanwhile, and `dropped` counts what its bound refused - so
        raise `capacity` for chatty work, and stream `logs()` for a
        tail with no end.
        """
        stream = (
            self._client.logs(state, capacity, level)
            if state is not None
            else self._client.process_logs(capacity, level)
        )
        it = stream.__aiter__()
        await anext(it)
        out = CapturedLogs(records=[])

        def take(records: list[LogRecord], dropped: int) -> None:
            out.records.extend(records)
            out.dropped = max(out.dropped, dropped)

        try:
            yield out
            if state is None:
                with anyio.move_on_after(settle):
                    async for records, dropped in it:
                        take(records, dropped)
                return
            barrier = await self._client.logs_barrier(state)
            async for records, dropped in it:
                for at, record in enumerate(records):
                    if (record.action() == "finalized"
                            and record.request() == barrier):
                        take(records[:at], dropped)
                        return
                take(records, dropped)
            raise RuntimeError(
                "the log stream ended before the barrier's marker arrived")
        finally:
            await stream.aclose()

    async def aclose(self) -> None:
        """Release the evaluators, then the stores, and report together.

        Detached objects are untracked, so this never touches them.
        A swept connection reports nothing: every release fails when
        the server already forgot the connection, and the handles are
        gone either way, which is the outcome this wanted. One failure
        on a live connection raises alone; several raise as an
        `ExceptionGroup`.
        """
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for state in tuple(self._evals):
            try:
                await self._release(state)
            except Exception as exc:
                errors.append(exc)
        self._evals.clear()
        for store in tuple(self._stores):
            try:
                await self._release(store)
            except Exception as exc:
                errors.append(exc)
        self._stores.clear()
        if errors and await self._client.alive():
            if len(errors) == 1:
                raise errors[0]
            raise ExceptionGroup("closing the session failed", errors)
        if errors:
            logger.warning("closing a swept connection; %d release(s) dropped", len(errors))
