"""
Client side of the remote layer.

The client owns the connection, the codec and the lifecycle calls.
Every object it hands back is a GENERATED class from
huggorm_generated.rpc: real methods, real signatures, one per
declared class, satisfying the same protocol the in-process
wrapper satisfies.

That is the whole of this module's knowledge of the domain: none. It
looks a class up by name in the generated registry and calls it. What
used to live here was a RemoteObj that resolved method names against
a spec table inside __getattr__ - which worked, and which a
typechecker could see nothing at all through. It could neither reject
a call to a method that does not exist nor check the arguments of one
that does, and it satisfied every Protocol vacuously.

Wire-values still come back as REAL local objects (copies,
deserialized through the private binding helpers); proxies stay remote
behind handles. Identical semantics to the in-process layer, different
location.

`protocol` holds the frames. One connection is one token, so `bind`
opens the connection.
"""

from __future__ import annotations

import contextlib
import itertools
import logging
import os
import threading
import weakref
from collections.abc import AsyncGenerator, AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import anyio

from huggorm_generated._callspec import Acquire, Call
from huggorm_generated._policy import ACQUIRE, FREE, NO_RPC

from . import views
from .codec import Codec
from .lifecycle import ShareMode
from .logbus import LOG_CAPACITY, LOG_LEVEL
from .protocol import (
    Channel,
    Control,
    Faults,
    Op,
    ProtocolError,
    Refusal,
    Refused,
    build_identity,
)

logger = logging.getLogger(__name__)

# How often a log reader asks for records when the last ask answered
# none. After a non-empty batch it asks again at once.
LOG_POLL = 0.05

# A log reader's class, read off the call that answers one: no layer
# above the bindings names a domain type.
_READER = FREE["subscribe_process_logs"].returns


class ConnectionExpired(RuntimeError):
    """The server no longer knows this connection.

    Raised once the ping loop learns the connection was swept, or a
    call is refused for it. Every handle the client held is gone with
    it - the leases were released when the sweeper ran - so the client
    stops rather than re-binding: a fresh bind would hand back a
    live-looking client whose every handle fails, which is the late,
    confusing failure this replaces (huggorm#49).

    Recovery is `bind()` plus re-acquiring, and that is the caller's
    decision because only the caller knows what it was holding."""


class ConnectionLost(ConnectionError):
    """The socket closed under a call. The server's leases are not
    released by that: a new connection that claims the token can
    take them back while they last."""


def _handle_of(obj: Any) -> str:
    """A proxy argument's handle id.

    A protocol-typed parameter admits an in-process object statically,
    so the location is checked here (huggorm#26): the server could not
    reach that object, and an AttributeError would not say why."""
    if not hasattr(obj, "handle_id"):
        raise TypeError(
            f"{type(obj).__name__} is not a handle on this server: a "
            f"remote call takes an object the server holds")
    if obj.handle_id is None:
        raise ValueError(f"this {type(obj).__name__} was already released")
    return str(obj.handle_id)


@dataclass
class _Pending:
    """One call waiting for its answer. `frame` stays None when the
    connection closed first."""

    done: anyio.Event = field(default_factory=anyio.Event)
    frame: list[Any] | None = None


class NixClient:
    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = os.fspath(path)
        self.codec = Codec()
        # Rebuilds a declared error from its parts, so a remote failure
        # has the same shape as an in-process one: an InternalError
        # whose __cause__ is the real error (huggorm#36).
        self.faults = Faults(self.codec)
        self.channel: Channel | None = None
        self.token: str | None = None
        # The task group this client's background work runs in: the
        # reader and the ping loop, each with its own cancel scope.
        # None until `__aenter__`, which is why `bind` outside the
        # context refuses rather than starting a task nothing owns.
        self._tasks: Any = None
        self._reader: Any = None
        self._pinger: Any = None
        self._expired = False
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        # How many live client objects point at each handle, and which
        # handles have lost their last one. A finalizer runs on
        # whichever thread dropped the reference - possibly during
        # interpreter shutdown - and cannot await, so it only takes the
        # lock and appends; the flush sends the frame (huggorm#28).
        self._refs: dict[str, int] = {}
        self._dropped: list[str] = []
        self._ref_lock = threading.Lock()

    def proxy(self, cls_name: str, handle_id: str) -> Any:
        """A client-side object for one remote handle.

        The class is generated - one per declared class, with
        the same inheritance - so this is a lookup and a constructor,
        and this module names nothing.

        The object is TRACKED: when the last one pointing at a handle
        goes away, the handle is queued for release. Two objects can
        name one handle (this method is public, and the lifecycle tests
        forge duplicates), so the count is per handle, not per object -
        otherwise the first drop would release a lease the second
        object is still using."""
        from huggorm_generated.rpc import RPC_CLASSES

        try:
            cls = RPC_CLASSES[cls_name]
        except KeyError:
            raise TypeError(
                f"{cls_name!r} has no generated client class; the build "
                f"offers {sorted(RPC_CLASSES)}") from None
        obj = cls(self, handle_id)
        self._track(obj, handle_id)
        return obj

    def view(self, cls_name: str, handle_id: str, contents: Any) -> Any:
        """A realized list or attribute set: the proxy for its handle,
        with what the walk carried readable locally (huggorm#147).
        Tracked as `proxy` tracks, so dropping it releases the
        handle."""
        from huggorm_generated.rpc import RPC_CLASSES

        mixin = views.AttrsView if isinstance(contents, dict) else views.ListView
        obj = views.view_class(mixin, RPC_CLASSES[cls_name])(
            self, handle_id, contents)
        self._track(obj, handle_id)
        return obj

    # -- dropped handles ------------------------------------------------
    def _track(self, obj: Any, handle_id: str) -> None:
        with self._ref_lock:
            self._refs[handle_id] = self._refs.get(handle_id, 0) + 1
        weakref.finalize(obj, self._forget, handle_id)

    def _forget(self, handle_id: str) -> None:
        """One client object for this handle is gone. Runs from a
        finalizer: no awaiting, no I/O, no assumptions about the
        thread. Queue it and return."""
        with self._ref_lock:
            n = self._refs.get(handle_id)
            if n is None:
                # Released explicitly already, and untracked there. The
                # finalizer still fires when the object dies; queueing
                # here would send the server an id it has forgotten.
                return
            if n > 1:
                self._refs[handle_id] = n - 1
                return
            del self._refs[handle_id]
            self._dropped.append(handle_id)

    def _untrack(self, handle_id: str) -> None:
        """Forget a handle released explicitly, so the finalizer that
        fires later does not queue an id the server no longer knows."""
        with self._ref_lock:
            self._refs.pop(handle_id, None)
            self._dropped[:] = [h for h in self._dropped if h != handle_id]

    async def flush_dropped(self) -> int:
        """Release every handle whose last client object went away, in
        one DROP frame, and answer how many it named.

        A handle re-acquired between the drop and this flush is skipped:
        _refs having an entry again means something is using it, and
        releasing it here would pull the lease out from under a live
        object.

        DROP has no answer. The server releases what it still knows
        and ignores the rest: by flush time a lease may already be
        gone - closed explicitly, transferred, or swept."""
        with self._ref_lock:
            queued = [h for h in self._dropped if h not in self._refs]
            self._dropped.clear()
        if not queued:
            return 0
        await self._live().send([Op.DROP, queued])
        return len(queued)

    # -- frames ---------------------------------------------------------
    def _live(self) -> Channel:
        if self._expired:
            raise ConnectionExpired(
                f"connection {self.token!r} was swept by the server; its "
                f"handles are gone. Call bind() and re-acquire.")
        if self.channel is None:
            raise ConnectionLost(f"no connection to {self.path}")
        return self.channel

    async def _ask(self, op: Op, *body: Any) -> Any:
        """Send one CALL or CONTROL and wait for its answer.

        A caller cancelled while it waits sends CANCEL, so the server
        stops the work nobody will read. The answer may still arrive,
        and the reader drops it."""
        channel = self._live()
        cid = next(self._ids)
        pending = self._pending[cid] = _Pending()
        try:
            try:
                await channel.send([op, cid, *body])
            except (anyio.BrokenResourceError,
                    anyio.ClosedResourceError) as e:
                raise ConnectionLost(
                    f"the connection to {self.path} is closed") from e
            await pending.done.wait()
        except anyio.get_cancelled_exc_class():
            if not pending.done.is_set():
                with contextlib.suppress(anyio.BrokenResourceError,
                                         anyio.ClosedResourceError):
                    await channel.send([Op.CANCEL, cid])
            raise
        finally:
            self._pending.pop(cid, None)
        match pending.frame:
            case [Op.RESULT, _, value]:
                return value
            case [Op.FAULT, _, fault]:
                raise self._fault(fault)
            case None:
                raise ConnectionLost(
                    f"the connection to {self.path} closed during the call")
        raise ProtocolError(f"an answer arrived as {pending.frame!r:.80}")

    def _fault(self, raw: Any) -> BaseException:
        error = self.faults.decode(raw)
        if isinstance(error, Refused) and error.reason is Refusal.SWEPT:
            self._expired = True
            return ConnectionExpired(
                f"connection {self.token!r} was swept by the server; its "
                f"handles are gone. Call bind() and re-acquire.")
        return error

    async def _read(self, channel: Channel, *,
                    task_status: Any = anyio.TASK_STATUS_IGNORED) -> None:
        """Hand each answer to the call waiting for it, until the
        connection closes. Then wake every call still waiting."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            try:
                while True:
                    frame = await channel.receive()
                    match frame:
                        case [Op.RESULT | Op.FAULT, int(cid), _]:
                            pending = self._pending.get(cid)
                            if pending is not None:
                                pending.frame = frame
                                pending.done.set()
                        case _:
                            raise ProtocolError(
                                f"a frame arrived as {frame!r:.80}")
            except (anyio.EndOfStream, anyio.BrokenResourceError,
                    anyio.ClosedResourceError):
                pass
            except ProtocolError:
                logger.warning("closing the connection to %s", self.path,
                               exc_info=True)
            finally:
                if self.channel is channel:
                    self.channel = None
                for pending in self._pending.values():
                    pending.done.set()
                with anyio.CancelScope(shield=True):
                    await channel.aclose()

    # -- connection lifecycle -----------------------------------------
    async def __aenter__(self) -> NixClient:
        """Open the scope this client's background work lives in.

        A client reads answers and PINGS, each in a task, and anyio
        starts a task only inside a task group - so the client has to
        own one. That is what makes `async with` mandatory rather than
        decorative (huggorm#35).

        The group is entered here and exited in `__aexit__`, which
        anyio requires to be the SAME task. That rules out a client
        handed to an `AsyncExitStack` in a pytest fixture, because
        the fixture and the test do not share a task. Measured, not
        assumed: the probe failed at teardown with an exception
        group."""
        self._tasks = anyio.create_task_group()
        await self._tasks.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> bool | None:
        """Stop the ping loop and the reader, and close the scope.

        Both are cancelled BEFORE the group is exited, because a task
        group waits for its children and neither returns on its own."""
        self.stop_pinging()
        self._close()
        tasks, self._tasks = self._tasks, None
        return await tasks.__aexit__(*exc)  # type: ignore[no-any-return]

    def _close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            self._reader = None

    async def bind(self, claim_token: str | None = None) -> str:
        """Connect and take a connection identity. A detached session's
        token as `claim_token` claims its escrowed handles. The server
        reports its lease TTL so pings can keep pace with it.

        A second bind opens a new connection, with a new token unless
        it claims one."""
        if self._tasks is None:
            raise RuntimeError(
                "this client is not open. A ping loop is a task and a "
                "task needs a scope, so a NixClient owns a task group "
                "and `bind` starts the loop inside it. Use "
                "`async with remote.connect(path) as client:` - or "
                "`async with NixClient(path)` if you are binding by hand.")
        self.stop_pinging()
        self._close()
        stream = await anyio.connect_unix(self.path)
        channel = Channel(stream)
        try:
            await channel.send([Op.HELLO, build_identity(), claim_token])
            match await channel.receive():
                case [Op.WELCOME, str(token), int() | float() as ttl]:
                    pass
                case [Op.FAULT, None, fault]:
                    raise self.faults.decode(fault)
                case frame:
                    raise ProtocolError(
                        f"the server answered HELLO with {frame!r:.80}")
        except BaseException:
            with anyio.CancelScope(shield=True):
                await channel.aclose()
            raise
        self.channel, self.token, self._expired = channel, token, False
        self._reader = await self._tasks.start(self._read, channel)
        interval = max(0.5, min(ttl / 4, 15)) if ttl > 0 else 10.0
        # `start`, not `start_soon`: it waits for the loop to report
        # its own cancel scope, so `stop_pinging` can never find
        # nothing to cancel.
        self._pinger = await self._tasks.start(self._ping_loop, interval)
        return token

    async def _ping_loop(
            self, interval: float = 10.0, *,
            task_status: Any = anyio.TASK_STATUS_IGNORED) -> None:
        """The loop, wrapped in the scope that stops it.

        `bind` starts this with `tg.start`, which waits for
        `task_status.started` and hands the scope back - so
        `stop_pinging` always has something to cancel.

        Awaiting it DIRECTLY also works, and a test does: the default
        `TASK_STATUS_IGNORED` swallows the report, and the scope is
        then entered and left by the awaiting task, which is what
        anyio requires. `_ping_forever` is the body split out so that
        call reads as one iteration of the real loop rather than as a
        task."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            await self._ping_forever(interval)

    async def _ping_forever(self, interval: float) -> None:
        while True:
            await anyio.sleep(interval)
            try:
                with anyio.fail_after(5):
                    ok = await self._ask(Op.CONTROL, Control.PING, [])
                if not ok:
                    # Swept. Say so once, loudly, and stop - the next
                    # call raises ConnectionExpired rather than
                    # failing later as "unknown handle" on something
                    # unrelated (huggorm#49).
                    self._expired = True
                    logger.warning(
                        "connection %r was swept by the server; its handles "
                        "are gone. Call bind() and re-acquire.", self.token)
                    return
                # The ping loop is the flush's home: it already runs on
                # the event loop on a timer, which is exactly what a
                # finalizer cannot do.
                with anyio.fail_after(5):
                    await self.flush_dropped()
            except ConnectionLost:
                # Nothing left to keep alive. The next call says so.
                return
            except Exception:
                # A blip is not a death: the server sweeps a client
                # that stays silent, and this loop is what keeps it
                # from doing so. Logged rather than swallowed, because
                # a library that goes quiet in someone else's process
                # is indistinguishable from one that is working.
                logger.warning("ping failed; retrying", exc_info=True)

    def stop_pinging(self) -> None:
        """Stop the ping loop, without closing the client.

        SYNCHRONOUS, because cancelling a scope is. A caller that just
        wants the client gone uses `async with` and never calls this;
        it stays for a test that needs the loop stopped mid-body, and
        for `__aexit__`, which has to stop the loop before the group
        waits for it."""
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None

    async def alive(self) -> bool:
        """Whether the server still knows this connection.

        One ping, answered once. The loop asks this on a timer; this
        asks it now, for a caller deciding whether a failed release is
        worth reporting: handles on a swept connection are already
        gone, which is the outcome a close wanted.

        Only a failure to REACH the server answers False. Anything
        else is a bug here, and it raises rather than reading as a
        swept connection that silences the errors it was asked about.
        """
        try:
            with anyio.fail_after(5):
                return bool(await self._ask(Op.CONTROL, Control.PING, []))
        except (ConnectionExpired, TimeoutError, OSError):
            return False

    async def share(self, obj: Any, to_token: str,
                    mode: ShareMode = ShareMode.COPY) -> None:
        await self._ask(Op.CONTROL, Control.SHARE,
                        [to_token, _handle_of(obj), mode.value])

    async def detach(self, obj: Any = None, all: bool = False) -> bool:
        """Hand our claim back as escrow under our own token. With no
        target and all=True, detaches every lease we hold."""
        hid = None if obj is None else _handle_of(obj)
        return bool(await self._ask(Op.CONTROL, Control.DETACH, [hid, all]))

    def _encode_args(self, spec: Call | Acquire, args: tuple[Any, ...] | list[Any]
                     ) -> list[Any]:
        return [self.codec.encode(a.type, v, _handle_of)
                for a, v in zip(spec.args, args, strict=False)]

    async def acquire(self, cls_name: str, *args: Any) -> Any:
        """Construct one instance remotely, from typed constructor
        arguments. The arguments cross exactly like method arguments -
        same codec, same wire policies - because they are declared the
        same way."""
        spec = ACQUIRE.get(cls_name)
        if spec is None:
            # Not every constructible class is remotely constructible.
            # One that crosses as a value has no handle to construct
            # INTO: build it locally and pass it as an argument.
            raise ValueError(
                f"{cls_name!r} cannot be constructed remotely; the bindings "
                f"offer {sorted(ACQUIRE)}")
        if len(args) < spec.required or len(args) > len(spec.args):
            raise TypeError(
                f"{cls_name} takes {spec.required}..{len(spec.args)} "
                f"argument(s) ({', '.join(a.name for a in spec.args)}), "
                f"got {len(args)}")
        hid = await self._ask(Op.CALL, spec.index, None,
                              self._encode_args(spec, args))
        return self.proxy(cls_name, hid)

    async def release(self, obj: Any) -> None:
        if obj.handle_id is None:
            raise ValueError("handle already released through this object")
        await self._ask(Op.CONTROL, Control.RELEASE, [obj.handle_id])
        self._untrack(obj.handle_id)
        obj.handle_id = None

    async def realize(self, obj: Any, depth: int = 0,
                      budget: int = 0, force: bool = False) -> Any:
        """A whole value tree in one round trip.

        Walking a value one call at a time costs a round trip and a
        thread handover per node. This asks the server to walk it once
        and hand back the shape: scalars as themselves, and a list or
        an attribute set as a read-only view (`views`) that is also the
        proxy for its node, so it can be handed back to Nix. An
        attribute set iterates in name order.

        Nothing is forced. A thunk comes back as a proxy, and so does
        every node the walk stopped at - past `depth`, past `budget`,
        or already seen elsewhere in the tree. Force one and realize it
        again to go further.

        depth and budget are both bounds because a tree is unbounded in
        two directions. depth counts levels EXPANDED, so 1 is the root
        alone and every child a proxy. budget stops it going wide,
        which is the one that actually bites: an attribute set can hold
        a hundred thousand entries one level down. Zero means the
        server's default.

        `force` forces each node the walk visits, inside the same
        bounds. It does not enter a derivation, and a node whose force
        throws comes back as a proxy whose first read raises that
        error, so the rest of the tree still arrives (huggorm#147)."""
        if obj.handle_id is None:
            raise ValueError("this handle was already released")
        raw = await self._ask(Op.CONTROL, Control.REALIZE,
                              [obj.handle_id, depth, budget, force])
        return self.codec.decode_tree(raw, self.proxy, self.view)

    async def value(self, state: Any, data: Any) -> Any:
        """A value `state` makes from Python data, in one round trip
        (huggorm#147).

        Data is None, bool, int, float, str, a list or tuple, a mapping
        with str keys, or a value handle - a realized view included - at
        any depth. Anything else is refused here, before anything is
        sent.

        Answers as `realize` does: a list or an attribute set is a
        read-only view with its own handle, and a scalar at the root is
        a handle. A handle in the data comes back as the object that was
        passed."""
        if state.handle_id is None:
            raise ValueError("this handle was already released")
        passed: dict[str, Any] = {}

        def handle_id(obj: Any) -> str:
            hid = _handle_of(obj)
            passed[hid] = obj
            return hid

        raw = await self._ask(Op.CONTROL, Control.BUILD, [
            state.handle_id, self.codec.encode_data(data, handle_id)])
        return self.codec.decode_tree(
            # Not `passed.get(hid) or`: an empty view is falsy.
            raw, lambda cls, hid: (passed[hid] if hid in passed
                                   else self.proxy(cls, hid)),
            self.view)

    async def logs(self, obj: Any, capacity: int = 0,
                   level: int | None = None) -> AsyncGenerator[
                       tuple[list[Any], int]]:
        """Records Nix raises while this state works, as they arrive.

        A reader is a handle the server opens on the state's own
        thread, and this asks it for records until the caller stops
        iterating. Stopping releases the reader, which leaves the
        subscription. A remote `unsubscribe_logs` leaves only what
        `subscribe_logs` opened, so it does not end this.

        It yields a BATCH, `(records, dropped)`, because the queue
        answers a batch. `dropped` is why it is a pair: the queue is
        bounded, and a client that cannot see a refusal cannot tell a
        quiet evaluation from a lost one. The count is cumulative, so
        it survives a batch the caller skipped.

        `capacity` 0 and `level` None take the defaults. None rather
        than 0, because level 0 is lvlError - a subscription somebody
        means, not an absent one.

        What it does NOT see is what `process_logs` does: a record
        raised on a fetcher thread, a file-transfer thread or a build
        belongs to no state's thread. The two do not overlap - a
        thread that subscribed claims its records - so a caller
        wanting everything reads both.

        MANY readers per state. A second subscription would replace
        the first on that state's thread, so the server opens ONE and
        fans it out - each reader gets its own view, its own capacity
        and its own level (huggorm#85).

        The FIRST batch is always empty, and it means the subscription
        is installed. A caller opens this to watch work it is about to
        start, so it needs a point where starting is safe; without the
        empty batch the first batch would be the first record, which
        arrives only after the work it was meant to report."""
        if obj.handle_id is None:
            raise ValueError("this handle was already released")
        async for batch in self._log_batches(obj.handle_id, capacity, level):
            yield batch

    async def process_logs(self, capacity: int = 0,
                           level: int | None = None) -> AsyncGenerator[
                               tuple[list[Any], int]]:
        """Records no subscribed thread claimed, as they arrive.

        The same reader with nothing to name. `logs` takes a state
        because the tap routes by THREAD and an EvalState owns one;
        this takes what a fetcher thread, a file-transfer thread or a
        build raised, and none of those belongs to a state
        (huggorm#85). A build's log is the one this exists for.

        MANY readers over ONE sink: every connection that asks reads
        the same subscription through its own view.

        Batches, `dropped` and the empty first batch all mean what
        they mean in `logs`."""
        async for batch in self._log_batches(None, capacity, level):
            yield batch

    async def _log_batches(self, hid: str | None, capacity: int,
                           level: int | None) -> AsyncGenerator[
                               tuple[list[Any], int]]:
        """Open a reader, poll it until the caller stops, then release
        it.

        Shielded on the way out, because a caller that stops by
        cancellation still owes the server the release."""
        assert _READER is not None
        reader = self.proxy(_READER.name, await self._ask(
            Op.CONTROL, Control.LOGS,
            [hid, capacity or LOG_CAPACITY,
             LOG_LEVEL if level is None else level]))
        try:
            yield [], 0
            while True:
                records = await reader.drain()
                if not records:
                    await anyio.sleep(LOG_POLL)
                    continue
                yield records, await reader.dropped()
        finally:
            with anyio.CancelScope(shield=True), contextlib.suppress(
                    ConnectionExpired, ConnectionLost):
                await reader.aclose()

    async def logs_barrier(self, obj: Any) -> int:
        """The request id whose "finalized" record ends `obj`'s records
        so far, on every `logs` reader of it.

        The server runs one call on the state's own thread, so its
        marker is queued after everything that thread raised before.
        A reader that sees the marker holds all of it."""
        if obj.handle_id is None:
            raise ValueError("this handle was already released")
        return int(await self._ask(Op.CONTROL, Control.LOGS_BARRIER,
                                   [obj.handle_id]))

    async def call_function(self, name: str, *args: Any) -> Any:
        """Call one of the bindings' module-level functions remotely.

        No handle: a free function has no instance. Otherwise identical
        to a method call, same codec and same policies."""
        spec = FREE.get(name)
        if spec is None:
            # Two different answers, and a caller can act on the
            # difference. A declared function the wire cannot carry is
            # a policy the build decided, and it says which.
            if name in NO_RPC:
                raise TypeError(
                    f"{name!r} has no RPC surface: {NO_RPC[name]}")
            raise ValueError(
                f"{name!r} is not a binding function; the bindings offer "
                f"{sorted(FREE)}")
        if len(args) != len(spec.args):
            raise TypeError(f"{name} takes {len(spec.args)} argument(s), "
                            f"got {len(args)}")
        return self._result(spec, await self._ask(
            Op.CALL, spec.index, None, self._encode_args(spec, args)))

    async def invoke(self, m: Call, handle_id: str | None,
                     args: list[Any]) -> Any:
        """Make one call.

        `m` is the spec the generated method carries - a `Call`, built
        at import from constants the emitter wrote. Nothing is
        resolved here: the build already did it.

        `-> Any` and it cannot be otherwise: the return type differs
        per call. The generated method casts it, which is where a
        caller's type comes from and where a typechecker checks it."""
        if handle_id is None:
            # release() blanks the id, so a call through a spent proxy
            # arrives here as None.
            raise ValueError(f"{m.name}: this handle was already released")
        if len(args) != len(m.args):
            raise TypeError(f"{m.name} takes {len(m.args)} argument(s), "
                            f"got {len(args)}")
        return self._result(m, await self._ask(
            Op.CALL, m.index, handle_id, self._encode_args(m, args)))

    def _result(self, spec: Call, raw: Any) -> Any:
        """Proxies stay remote behind a handle; values come back as
        real local objects."""
        returned = spec.returns
        return self.codec.decode(
            returned, raw,
            lambda hid: self.proxy(returned.name if returned else "", hid))


@contextlib.asynccontextmanager
async def connect(path: str | os.PathLike[str],
                  claim: str | None = None) -> AsyncIterator[NixClient]:
    """Connect to the server at `path` and bind a connection identity,
    claiming escrow when a detached session's token is presented.

    A context manager, because a client runs a reader and a ping loop,
    each a task, and anyio starts a task only inside a task group - so
    something has to hold the scope open, and the client is the only
    thing with the right lifetime (huggorm#35). The loops cannot
    outlive the client and cannot be forgotten."""
    async with NixClient(path) as client:
        await client.bind(claim)
        yield client
