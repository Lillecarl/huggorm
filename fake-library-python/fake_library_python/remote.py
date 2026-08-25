"""
Client side of the remote layer.

RemoteObj gives the OOP feel: it holds a handle and exposes the same
method surface as the local async wrapper. Wire-values come back as
REAL local objects (copies - deserialized via the private binding
helpers), proxies stay remote behind handles. Identical semantics to
the in-process layer, different location.
"""

import asyncio

import grpclib
import grpclib.client
import grpclib.const
import grpclib.exceptions
import json
from google.protobuf import message_factory

from . import grpc_pb as schema
from .lifecycle import TOKEN_HEADER
from .wire import WireCodec


class RemoteObj:
    def __init__(self, client, cls_name: str, handle_id: str):
        self._client = client
        self._cls = cls_name
        self.handle_id = handle_id

    def _proto(self) -> dict:
        mft = self._client.manifest
        try:
            return mft["wrappers"].get(self._cls) or mft["returned_types"][self._cls]
        except KeyError:
            raise AttributeError(
                f"{self._cls!r} is not a class in the manifest") from None

    @property
    def wire(self) -> str:
        # Both groups, not just wrappers: a returned type used to fall
        # through to the "proxy" default and answer correctly only
        # because every returned proxy happens to be one.
        return self._proto()["wire"]

    def __getattr__(self, method):
        if method.startswith("_"):
            # Never let a dunder lookup (copy, pickle, repr helpers) walk
            # into manifest resolution and come back as a coroutine.
            raise AttributeError(method)
        proto = self._proto()
        m = next((m for m in proto["methods"] if m["name"] == method), None)
        if m is None:
            # next() with no default raised StopIteration here, which
            # neither reads as a missing attribute nor survives inside a
            # coroutine.
            raise AttributeError(
                f"{self._cls!r} has no remote method {method!r}; the manifest "
                f"offers {sorted(x['name'] for x in proto['methods'])}")

        async def call(*args):
            return await self._client.invoke(self._cls, m, self.handle_id, args)

        return call


class NixClient:
    def __init__(self, host="127.0.0.1", port=50051):
        self.pool = schema.load_pool()
        self.manifest = schema.load_manifest()
        self.codec = WireCodec(self.manifest)
        self.channel = grpclib.client.Channel(host, port)
        self.token: str | None = None
        self._pinger: asyncio.Task | None = None

        def msg(name):
            return message_factory.GetMessageClass(
                self.pool.FindMessageTypeByName(f"{schema.PKG}.{name}"))

        self.msg = msg

    async def _rpc(self, path, req, reply_name):
        from fake_library_generated._runtime import WrapperError

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
            # Typed wrapper errors cross as JSON in the status message.
            # Decoding lives HERE so every rpc - Session lifecycle
            # included - rebuilds real errors instead of leaking
            # transport exceptions. No `from` clause: the rebuilt error
            # keeps the decoded cause as __cause__; the GRPCError stays
            # visible as __context__.
            try:
                d = json.loads(e.message)
                if isinstance(d, dict) and "code" in d:
                    raise WrapperError.from_dict(d)
            except (ValueError, TypeError):
                pass
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
        return resp.token

    async def _ping_loop(self, interval=10.0):
        while True:
            await asyncio.sleep(interval)
            try:
                req = self.msg("PingReq")(token=self.token)
                await asyncio.wait_for(
                    self._rpc(f"/{schema.PKG}.Session/Ping", req, "AckResp"), 5)
            except Exception:
                pass  # keep trying; the server sweeps us if we stay silent

    def stop_pinging(self):
        if self._pinger is not None:
            self._pinger.cancel()
            self._pinger = None

    async def share(self, obj: RemoteObj, to_token: str, mode="copy"):
        req = self.msg("ShareReq")(mode=mode)
        req.handle.id = obj.handle_id
        req.to_token = to_token
        await self._rpc(f"/{schema.PKG}.Session/Share", req, "AckResp")

    async def detach(self, obj: RemoteObj | None = None, all=False) -> bool:
        """Hand our claim back as escrow under our own token. With no
        target and all=True, detaches every lease we hold."""
        req = self.msg("DetachReq")(all=all)
        if obj is not None:
            req.target.id = obj.handle_id
        resp = await self._rpc(f"/{schema.PKG}.Session/Detach", req, "AckResp")
        return resp.ok

    async def acquire(self, cls_name) -> RemoteObj:
        req = self.msg("AcquireReq")()
        setattr(req, "class", cls_name)
        resp = await self._rpc(f"/{schema.PKG}.Session/Acquire", req, "Handle")
        return RemoteObj(self, cls_name, resp.id)

    async def release(self, obj: RemoteObj):
        if obj.handle_id is None:
            # Releasing twice through the same object used to send an
            # empty id: protobuf drops a None string, so the server
            # answered "no lease on ''" and the caller saw a plausible
            # error about the wrong handle.
            raise ValueError("handle already released through this object")
        req = self.msg("Handle")(id=obj.handle_id)
        await self._rpc(f"/{schema.PKG}.Session/Release", req, "Handle")
        obj.handle_id = None

    async def invoke(self, cls_name, m, handle_id, args):
        req = self.msg(m["rpc"]["req"])()
        req.self.id = handle_id

        # Wire names and wire policies both come out of the manifest, so
        # this method mentions no concrete type: a proxy arg contributes
        # its handle id, a wire-value serializes through its declared
        # parts, a scalar goes in as itself.
        for p, val in zip(m["params"], args):
            self.codec.encode(req, p["name"], p["type"], val,
                              lambda obj: obj.handle_id)

        resp = await self._rpc(m["rpc"]["path"], req, m["rpc"]["resp"])

        # Proxies stay remote behind a handle; values come back as real
        # local objects.
        return self.codec.decode(
            resp, "result", m["return_type"],
            lambda hid: RemoteObj(self, m["return_type"], hid))


async def connect(host="127.0.0.1", port=50051, claim=None) -> NixClient:
    """Connect and bind a connection identity (claiming escrow when a
    detached session's token is presented)."""
    client = NixClient(host, port)
    await client.bind(claim)
    return client
