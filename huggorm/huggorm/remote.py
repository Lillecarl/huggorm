"""
Client side of the remote layer.

The client owns the connection, the codec and the lifecycle rpcs.
Every object it hands back is a GENERATED class from
huggorm_generated.rpc: real methods, real signatures, one per
class in the manifest, satisfying the same protocol the in-process
wrapper satisfies.

That is the whole of this module's knowledge of the domain: none. It
looks a class up by name in the generated registry and calls it. What
used to live here was a RemoteObj that resolved method names against
the manifest inside __getattr__ - which worked, and which a
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

import grpclib
import grpclib.client
import grpclib.const
import grpclib.exceptions
from google.protobuf import message_factory

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


class NixClient:
    def __init__(self, host: str = "127.0.0.1", port: int = 50051) -> None:
        self.pool = schema.load_pool()
        self.manifest = schema.load_manifest()
        self.codec = WireCodec(self.manifest)
        # Rebuilds a declared error from the status details, so a
        # remote failure has the same shape as an in-process one: an
        # InternalError whose __cause__ is the real error (tasks/036).
        self.faults = FaultCodec(self.manifest, self.pool)
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

        The class is generated - one per class in the manifest, with
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
            self._pinger = asyncio.create_task(self._ping_loop(interval))
        return str(resp.token)

    async def _ping_loop(self, interval: float = 10.0) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                req = self.msg("PingReq")()
                ack = await asyncio.wait_for(
                    self._rpc(f"/{schema.PKG}.Session/Ping", req, "AckResp"), 5)
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
                await asyncio.wait_for(self.flush_dropped(), 5)
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
        offered = sorted(n for n, p in self.manifest["wrappers"].items()
                         if "acquire" in p)
        proto = self.manifest["wrappers"].get(cls_name)
        if proto is None or "acquire" not in proto:
            # Not every constructible class is remotely constructible.
            # One that crosses as a value has no handle to construct
            # INTO: build it locally and pass it as an argument.
            raise ValueError(
                f"{cls_name!r} cannot be constructed remotely; the manifest "
                f"offers {offered}")
        ctor = proto["ctor"]
        required = [p["name"] for p in ctor if p["default"] is None]
        if len(args) < len(required) or len(args) > len(ctor):
            raise TypeError(
                f"{cls_name} takes {len(required)}..{len(ctor)} argument(s) "
                f"({', '.join(p['name'] for p in ctor)}), got {len(args)}")

        req = self.msg(proto["acquire"]["req"])()
        for p, val in zip(ctor, args, strict=False):
            if val is None:
                continue  # optional, left at the proto3 default
            self.codec.encode(req, p["name"], p["type"], val,
                              lambda obj: obj.handle_id)
        resp = await self._rpc(proto["acquire"]["path"], req, "Handle")
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

    async def call_function(self, name: str, *args: Any) -> Any:
        """Call one of the bindings' module-level functions remotely.

        No handle: a free function has no instance. Otherwise identical
        to a method call, same codec and same policies."""
        proto = self.manifest.get("free_functions", {}).get(name)
        if proto is None:
            raise ValueError(
                f"{name!r} is not a binding function; the manifest offers "
                f"{sorted(self.manifest.get('free_functions', {}))}")
        if "rpc" not in proto:
            raise TypeError(
                f"{name!r} has no RPC surface: "
                + "; ".join(proto["wire_blockers"]))

        req = self.msg(proto["rpc"]["req"])()
        for p, val in zip(proto["params"], args, strict=True):
            self.codec.encode(req, p["name"], p["type"], val,
                              lambda obj: obj.handle_id)
        resp = await self._rpc(proto["rpc"]["path"], req, proto["rpc"]["resp"])
        return self.codec.decode(
            resp, "result", proto["return_type"],
            lambda hid: self.proxy(proto["return_type"], hid))

    async def invoke(self, m: dict[str, Any], handle_id: str | None,
                     args: list[Any]) -> Any:
        """Make one call. `m` is the call spec a generated method
        carries: the manifest's own entry for that method, minus the
        docstring. Nothing is resolved here - the build already did
        it."""
        if handle_id is None:
            # release() blanks the id, so a call through a spent proxy
            # arrives here as None. Protobuf would refuse it with a
            # message about a str field; this says what happened.
            raise ValueError(
                f"{m['name']}: this handle was already released")
        req = self.msg(m["rpc"]["req"])()
        req.self.id = handle_id

        # Wire names and wire policies both come out of the manifest, so
        # this method mentions no concrete type: a proxy arg contributes
        # its handle id, a wire-value serializes through its declared
        # parts, a scalar goes in as itself.
        for p, val in zip(m["params"], args, strict=True):
            self.codec.encode(req, p["name"], p["type"], val,
                              lambda obj: obj.handle_id)

        resp = await self._rpc(m["rpc"]["path"], req, m["rpc"]["resp"])

        # Proxies stay remote behind a handle; values come back as real
        # local objects.
        return self.codec.decode(
            resp, "result", m["return_type"],
            lambda hid: self.proxy(m["return_type"], hid))


async def connect(host: str = "127.0.0.1", port: int = 50051,
                  claim: str | None = None) -> NixClient:
    """Connect and bind a connection identity (claiming escrow when a
    detached session's token is presented)."""
    client = NixClient(host, port)
    await client.bind(claim)
    return client
