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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, overload, runtime_checkable

import anyio

from huggorm_generated import AsyncEvalState, AsyncStore, RPCEvalState, RPCStore

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
        capacity: int = 1024,
        level: int = 3,
        poll: float = 0.05,
    ) -> AsyncIterator[LogBatch]:
        """Records raised on this state's thread, as they arrive."""
        ...

    def capture(
        self,
        state: AsyncEvalState,
        capacity: int = 1024,
        level: int = 3,
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
        capacity: int = 1024,
        level: int = 3,
        poll: float = 0.05,
    ) -> AsyncIterator[LogBatch]:
        """Records raised on this state's thread, as they arrive.

        Polls the subscription's queue: a drain never blocks, so
        there is nothing to wait on, only a queue to re-read.
        `capacity` and `level` mean what `subscribe_logs` says.
        Empty polls yield nothing: quiet means no batch.

        Any state's records read here, not only one this session
        made. Watching creates no lease, so there is nothing to own.
        Stopping the iteration ends the subscription with it.
        """
        stream = await state.subscribe_logs(capacity, level)
        try:
            while True:
                records = await stream.drain()
                if records:
                    yield (records, await stream.dropped())
                await anyio.sleep(poll)
        finally:
            await stream.close()
            await state.unsubscribe_logs()

    @contextlib.asynccontextmanager
    async def capture(
        self,
        state: AsyncEvalState,
        capacity: int = 1024,
        level: int = 3,
    ) -> AsyncIterator[CapturedLogs]:
        """Collect what this state says while the block runs.

        Subscribing installs synchronously, so everything the block
        raises is already queued when it ends, and one last drain
        collects it: complete, not eventual. The `finally` drains on
        the way out however the block ends, and ends the
        subscription with it.
        """
        stream = await state.subscribe_logs(capacity, level)
        out = CapturedLogs(records=[])
        try:
            yield out
        finally:
            out.records.extend(await stream.drain())
            out.dropped = max(out.dropped, await stream.dropped())
            await stream.close()
            await state.unsubscribe_logs()

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
        for state in tuple(self._evals):
            try:
                await state.aclose()
            except Exception as exc:
                errors.append(exc)
        self._evals.clear()
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
        build_store: Store | None = None,
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
    ) -> AsyncIterator[LogBatch]:
        """Records raised on this state's server thread, as they arrive."""
        ...

    def process_logs(
        self,
        capacity: int = 0,
        level: int | None = None,
    ) -> AsyncIterator[LogBatch]:
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
        build_store: Store | None = None,
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
    ) -> AsyncIterator[LogBatch]:
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
    ) -> AsyncIterator[LogBatch]:
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
        subscription is installed before the block starts. When the
        block ends it keeps reading for `settle` seconds: records
        cross a socket, so "the queue holds everything" is true only
        after the last one lands. `state=None` captures the process
        stream instead.

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
        try:
            yield out
            with anyio.move_on_after(settle):
                async for records, dropped in it:
                    out.records.extend(records)
                    if dropped > out.dropped:
                        out.dropped = dropped
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
