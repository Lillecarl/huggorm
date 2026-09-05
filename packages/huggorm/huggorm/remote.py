"""
Client side of the remote layer.

The client owns the connection, the codec and the lifecycle rpcs.
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
"""

import asyncio
import logging
import threading
import weakref
from collections.abc import Callable
from typing import Any

import anyio
import grpclib
import grpclib.client
import grpclib.const
import grpclib.exceptions
from google.protobuf import message_factory

from huggorm_generated._callspec import Call
from huggorm_generated._policy import ACQUIRE, FREE, NO_RPC

from . import grpc_pb as schema
from .faults import FaultCodec, SchemaStatusDetails
from .lifecycle import TOKEN_HEADER
from .wire import WireCodec

logger = logging.getLogger(__name__)


class ConnectionExpired(RuntimeError):
    """The server no longer knows this connection.

    Raised once the ping loop learns the connection was swept. Every
    handle the client held is gone with it - the leases were released
    when the sweeper ran - so the client stops rather than re-binding:
    a fresh bind would hand back a live-looking client whose every
    handle fails, which is the late, confusing failure this replaces
    (tasks/049).

    Recovery is `bind()` plus re-acquiring, and that is the caller's
    decision because only the caller knows what it was holding."""


def _no_proxy(handle_id: str) -> Any:
    """The proxy arm of a decode that has none.

    Every record on the log stream is a wire VALUE, so `decode` never
    reaches this. Raising says so; a `lambda hid: None` would have
    turned a schema that drifted into a batch of Nones."""
    raise TypeError(
        f"handle {handle_id[:8]} arrived where only wire values were "
        f"expected")


class NixClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 50051) -> None:
        self.pool = schema.load_pool()
        self.codec = WireCodec()
        # Rebuilds a declared error from the status details, so a
        # remote failure has the same shape as an in-process one: an
        # InternalError whose __cause__ is the real error (tasks/036).
        self.faults = FaultCodec(self.pool)
        self.channel = grpclib.client.Channel(
            host, port, status_details_codec=SchemaStatusDetails(self.pool))
        self.token: str | None = None
        self._pinger: asyncio.Task[None] | None = None
        self._expired = False
        # How many live client objects point at each handle, and which
        # handles have lost their last one. A finalizer runs on
        # whichever thread dropped the reference - possibly during
        # interpreter shutdown - and cannot await, so it only takes the
        # lock and appends; the flush does the rpc (tasks/028).
        self._refs: dict[str, int] = {}
        self._dropped: list[str] = []
        self._ref_lock = threading.Lock()

        def msg(name: str) -> Any:
            return message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
                self.pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
                    f"{schema.PKG}.{name}"))

        self.msg: Callable[[str], Any] = msg

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

    # -- dropped handles ------------------------------------------------
    def _track(self, obj: Any, handle_id: str) -> None:
        with self._ref_lock:
            self._refs[handle_id] = self._refs.get(handle_id, 0) + 1
        weakref.finalize(obj, self._forget, handle_id)

    def _forget(self, handle_id: str) -> None:
        """One client object for this handle is gone. Runs from a
        finalizer: no awaiting, no rpc, no assumptions about the
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
        """Release every handle whose last client object went away.

        A handle re-acquired between the drop and this flush is skipped:
        _refs having an entry again means something is using it, and
        releasing it here would pull the lease out from under a live
        object."""
        with self._ref_lock:
            queued = [h for h in self._dropped if h not in self._refs]
            self._dropped.clear()
        if not queued:
            return 0
        req = self.msg("ReleaseManyReq")()
        for hid in queued:
            req.handles.add().id = hid
        resp = await self._rpc(
            f"/{schema.PKG}.Session/ReleaseMany", req, "ReleaseManyResp")
        # protobuf fields are Any; the schema says what this one is.
        return int(resp.released)

    async def _rpc(self, path: str, req: Any, reply_name: str) -> Any:
        if self._expired:
            raise ConnectionExpired(
                f"connection {self.token!r} was swept by the server; its "
                f"handles are gone. Call bind() and re-acquire.")
        Reply = self.msg(reply_name)
        metadata = {TOKEN_HEADER: self.token} if self.token else None
        stream = self.channel.request(
            path, grpclib.const.Cardinality.UNARY_UNARY, type(req), Reply,
            metadata=metadata)
        try:
            async with stream as s:
                await s.send_message(req)
                await s.end()
                return await s.recv_message()
        except grpclib.exceptions.GRPCError as e:
            # A typed failure crosses in the status DETAILS. Decoding
            # lives HERE so every rpc - Session lifecycle included -
            # rebuilds real errors instead of leaking transport
            # exceptions. No `from` clause: the rebuilt error keeps the
            # decoded cause as __cause__; the GRPCError stays visible
            # as __context__.
            rebuilt = self.faults.rebuild(e.details)
            if rebuilt is not None:
                # No `from`: the rebuilt error already carries the
                # decoded cause as __cause__, and Python sets the
                # GRPCError as __context__, so both stay visible.
                raise rebuilt  # noqa: B904
            raise

    # -- connection lifecycle -----------------------------------------
    async def bind(self, claim_token: str | None = None) -> str:
        """Adopt or create a connection identity; claims escrowed
        handles when presenting a detached session's token. The server
        reports its lease TTL so pings can keep pace with it."""
        req = self.msg("BindReq")()
        if claim_token:
            req.claim_token = claim_token
        resp = await self._rpc(f"/{schema.PKG}.Session/Bind", req, "ConnResp")
        was_new = self.token is None or self._pinger is None
        self.token = resp.token
        ttl = getattr(resp, "lease_ttl", 0) or 0
        interval = max(0.5, min(ttl / 4, 15)) if ttl > 0 else 10.0
        if was_new:
            if self._pinger is not None:
                self._pinger.cancel()
            # THE ONE asyncio SPAWN LEFT, and it is not an oversight.
            # anyio starts a task only inside a task group, and a task
            # group is a scope somebody has to hold open. A
            # `NixClient` has none: it is built by `connect()` and
            # stopped by a synchronous `stop_pinging()`, so there is
            # no `async with` to own the loop.
            #
            # Giving the client one is the fix, and it is a BREAKING
            # change to a surface other people use - which `CLAUDE.md`
            # says is the expensive kind. So it is Carl's call, and it
            # is written down rather than guessed (`tasks/092`).
            self._pinger = asyncio.create_task(self._ping_loop(interval))
        return str(resp.token)

    async def _ping_loop(self, interval: float = 10.0) -> None:
        while True:
            await anyio.sleep(interval)
            try:
                req = self.msg("PingReq")()
                with anyio.fail_after(5):
                    ack = await self._rpc(
                        f"/{schema.PKG}.Session/Ping", req, "AckResp")
                if not ack.ok:
                    # Swept. Say so once, loudly, and stop - the next
                    # call raises ConnectionExpired rather than
                    # failing later as "unknown handle" on something
                    # unrelated (tasks/049).
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
            except Exception:
                # A blip is not a death: the server sweeps a client
                # that stays silent, and this loop is what keeps it
                # from doing so. Logged rather than swallowed, because
                # a library that goes quiet in someone else's process
                # is indistinguishable from one that is working.
                logger.warning("ping failed; retrying", exc_info=True)

    def stop_pinging(self) -> None:
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None

    async def share(self, obj: Any, to_token: str,
                    mode: str = "copy") -> None:
        req = self.msg("ShareReq")(mode=mode)
        req.handle.id = obj.handle_id
        req.to_token = to_token
        await self._rpc(f"/{schema.PKG}.Session/Share", req, "AckResp")

    async def detach(self, obj: Any = None, all: bool = False) -> bool:
        """Hand our claim back as escrow under our own token. With no
        target and all=True, detaches every lease we hold."""
        req = self.msg("DetachReq")(all=all)
        if obj is not None:
            req.target.id = obj.handle_id
        resp = await self._rpc(f"/{schema.PKG}.Session/Detach", req, "AckResp")
        return bool(resp.ok)

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

        req = self.msg(spec.req)()
        for a, val in zip(spec.args, args, strict=False):
            if val is None:
                continue  # optional, left at the proto3 default
            self.codec.encode(req, a.name, a.type, val,
                              lambda obj: obj.handle_id)
        resp = await self._rpc(spec.path, req, "Handle")
        return self.proxy(cls_name, resp.id)

    async def release(self, obj: Any) -> None:
        if obj.handle_id is None:
            # Releasing twice through the same object used to send an
            # empty id: protobuf drops a None string, so the server
            # answered "no lease on ''" and the caller saw a plausible
            # error about the wrong handle.
            raise ValueError("handle already released through this object")
        req = self.msg("Handle")(id=obj.handle_id)
        await self._rpc(f"/{schema.PKG}.Session/Release", req, "Handle")
        self._untrack(obj.handle_id)
        obj.handle_id = None

    async def realize(self, obj: Any, depth: int = 0,
                      budget: int = 0) -> Any:
        """A whole value tree in one round trip.

        Walking a value one call at a time costs a round trip and a
        thread handover per node. This asks the server to walk it once
        and hand back the shape: scalars as themselves, a list as a
        list, an attribute set as a dict in name order.

        Nothing is forced. A thunk comes back as a proxy, and so does
        every node the walk stopped at - past `depth`, past `budget`,
        or already seen elsewhere in the tree. Force one and realize it
        again to go further.

        depth and budget are both bounds because a tree is unbounded in
        two directions. depth counts levels EXPANDED, so 1 is the root
        alone and every child a proxy. budget stops it going wide,
        which is the one that actually bites: an attribute set can hold
        a hundred thousand entries one level down. Zero means the
        server's default."""
        if obj.handle_id is None:
            raise ValueError("this handle was already released")
        req = self.msg("RealizeReq")()
        req.handle.id = obj.handle_id
        req.depth, req.budget = depth, budget
        resp = await self._rpc(f"/{schema.PKG}.Session/Realize", req,
                               "RealizeResp")
        return self.codec.tree_from_msg(resp.root, self.proxy)

    async def logs(self, obj: Any, capacity: int = 0,
                   level: int | None = None) -> Any:
        """Records Nix raises while this state works, as they arrive.

        The only rpc that is not a question. It opens a subscription
        on the state's own thread and then yields whatever the queue
        answers until the caller stops iterating - which is what closes
        the stream, and what ends the subscription with it.

        It yields a BATCH, `(records, dropped)`, because the queue
        answers a batch: one drain is one message, and fanning a drain
        of forty into forty messages would restate the shape rather
        than carry it.

        `dropped` is why it is a pair. The queue is bounded, so a full
        one refuses a message - and a client that cannot see that
        cannot tell a quiet evaluation from a lost one. The count is
        cumulative, so it survives a batch the caller skipped.

        `capacity` and `level` mean what `_log_options` says.

        What it does NOT see is what `process_logs` does: a record
        raised on a fetcher thread, a file-transfer thread or a build
        belongs to no state's thread, so it reaches this stream
        never. The two do not overlap - a thread that subscribed
        claims its records - so a caller wanting everything reads
        both.

        MANY readers per state. A second subscription would replace
        the first on that state's thread, so the server opens ONE and
        fans it out - each stream gets its own view, its own capacity
        and its own level (`tasks/085`). This was a refusal until
        2026-09-04.

        The FIRST batch is always empty, and it means the subscription
        is installed. A caller opens this to watch work it is about to
        start, so it needs a point where starting is safe; without the
        empty batch the first message would be the first record, which
        arrives only after the work it was meant to report."""
        if obj.handle_id is None:
            raise ValueError("this handle was already released")
        req = self.msg("LogsReq")()
        req.state.id = obj.handle_id
        self._log_options(req, capacity, level)
        async for batch in self._log_stream("Logs", req):
            yield batch

    async def process_logs(self, capacity: int = 0,
                           level: int | None = None) -> Any:
        """Records no subscribed thread claimed, as they arrive.

        The same stream with nothing to name. `logs` takes a state
        because the tap routes by THREAD and an EvalState owns one;
        this takes what a fetcher thread, a file-transfer thread or a
        build raised, and none of those belongs to a state
        (`tasks/085`). A build's log is the one this exists for.

        NO handle, so a connection with no state at all can open it -
        which is right, because the records it carries are the ones no
        handle could have reached.

        NOT everything in the process. A thread that subscribed claims
        its records, so a state with its own `logs` stream does not
        appear here. Reading both is how a caller sees all of it, and
        neither repeats the other.

        MANY readers over ONE sink. There is a single process-wide
        sink, so every connection that asks reads the same
        subscription through its own view - the fan-out `logs` uses,
        for the same reason. This was a refusal until 2026-09-04, and
        the reader it refused was a CLI printing everything as it
        happens.

        Batches, `dropped` and the empty first message all mean what
        they mean in `logs`."""
        req = self.msg("ProcessLogsReq")()
        self._log_options(req, capacity, level)
        async for batch in self._log_stream("ProcessLogs", req):
            yield batch

    @staticmethod
    def _log_options(req: Any, capacity: int, level: int | None) -> None:
        """The two options, set the same way on either request.

        `capacity` 0 takes the binding's default. `level` None does
        too, and None rather than 0 because level 0 is lvlError - a
        subscription somebody means, not an absent one."""
        req.capacity = capacity
        if level is not None:
            req.level = level

    async def _log_stream(self, rpc: str, req: Any) -> Any:
        """One log rpc, opened and drained until the caller stops.

        Both log streams run this. What they do differently is the
        request they build; everything after that - the token, the
        decode, the typed-failure contract - is one shape, and a
        second copy would be a second place for one of them to
        drift."""
        if self._expired:
            raise ConnectionExpired(
                f"connection {self.token!r} was swept by the server; its "
                f"handles are gone. Call bind() and re-acquire.")
        metadata = {TOKEN_HEADER: self.token} if self.token else None
        stream = self.channel.request(
            f"/{schema.PKG}.Session/{rpc}",
            grpclib.const.Cardinality.UNARY_STREAM,
            type(req), self.msg("LogsResp"), metadata=metadata)
        try:
            async with stream as s:
                await s.send_message(req)
                await s.end()
                async for resp in s:
                    # Through `decode`, so the client reads the field
                    # by the same declared type the server wrote it
                    # by. A LogRecord is a wire value, so the proxy
                    # arm is unreachable and says so.
                    yield (self.codec.decode(
                        resp, "records", "list[LogRecord]", _no_proxy),
                        int(resp.dropped))
        except grpclib.exceptions.GRPCError as e:
            # Same contract as _rpc: a typed failure rebuilds into the
            # error it was. A refusal carries no details and stays a
            # GRPCError, which is the honest shape for one.
            rebuilt = self.faults.rebuild(e.details)
            if rebuilt is not None:
                raise rebuilt  # noqa: B904
            raise

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

        req = self.msg(spec.req)()
        for a, val in zip(spec.args, args, strict=True):
            self.codec.encode(req, a.name, a.type, val,
                              lambda obj: obj.handle_id)
        resp = await self._rpc(spec.path, req, spec.resp)
        return self.codec.decode(
            resp, "result", spec.returns,
            lambda hid: self.proxy(spec.returns, hid))

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
            # arrives here as None. Protobuf would refuse it with a
            # message about a str field; this says what happened.
            raise ValueError(
                f"{m.path}: this handle was already released")
        req = self.msg(m.req)()
        req.self.id = handle_id

        # Wire names and wire policies both come out of the manifest, so
        # this method mentions no concrete type: a proxy arg contributes
        # its handle id, a wire-value serializes through its declared
        # parts, a scalar goes in as itself.
        for p, val in zip(m.args, args, strict=True):
            self.codec.encode(req, p.name, p.type, val,
                              lambda obj: obj.handle_id)

        resp = await self._rpc(m.path, req, m.resp)

        # Proxies stay remote behind a handle; values come back as real
        # local objects.
        return self.codec.decode(
            resp, "result", m.returns,
            lambda hid: self.proxy(m.returns, hid))


async def connect(host: str = "127.0.0.1", port: int = 50051,
                  claim: str | None = None) -> NixClient:
    """Connect and bind a connection identity (claiming escrow when a
    detached session's token is presented)."""
    client = NixClient(host, port)
    await client.bind(claim)
    return client
