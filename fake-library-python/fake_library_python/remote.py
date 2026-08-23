"""
Client side of the remote layer.

RemoteObj gives the OOP feel: it holds a handle and exposes the same
method surface as the local async wrapper. Wire-values come back as
REAL local objects (copies - deserialized via the private binding
helpers), proxies stay remote behind handles. Identical semantics to
the in-process layer, different location.
"""

import grpclib
import grpclib.client
import grpclib.const
import grpclib.exceptions
import json
from google.protobuf import message_factory

from . import grpc_pb as schema


class RemoteObj:
    def __init__(self, client, cls_name: str, handle_id: str):
        self._client = client
        self._cls = cls_name
        self.handle_id = handle_id

    @property
    def wire(self) -> str:
        return self._client.manifest["wrappers"].get(self._cls, {}).get("wire", "proxy")

    def __getattr__(self, method):
        mft = self._client.manifest
        proto = mft["wrappers"].get(self._cls) or mft["returned_types"][self._cls]
        m = next(m for m in proto["methods"] if m["name"] == method)

        async def call(*args):
            return await self._client.invoke(self._cls, m, self.handle_id, args)

        return call


class NixClient:
    def __init__(self, host="127.0.0.1", port=50051):
        self.pool = schema.load_pool()
        self.manifest = schema.load_manifest()
        self.channel = grpclib.client.Channel(host, port)

        def msg(name):
            return message_factory.GetMessageClass(
                self.pool.FindMessageTypeByName(f"{schema.PKG}.{name}"))

        self.msg = msg

    async def _rpc(self, path, req, reply_name):
        Reply = self.msg(reply_name)
        stream = self.channel.request(
            path, grpclib.const.Cardinality.UNARY_UNARY, type(req), Reply)
        async with stream as s:
            await s.send_message(req)
            await s.end()
            return await s.recv_message()

    async def acquire(self, cls_name) -> RemoteObj:
        req = self.msg("AcquireReq")()
        setattr(req, "class", cls_name)
        resp = await self._rpc(f"/{schema.PKG}.Session/Acquire", req, "Handle")
        return RemoteObj(self, cls_name, resp.id)

    async def release(self, obj: RemoteObj):
        req = self.msg("Handle")(id=obj.handle_id)
        await self._rpc(f"/{schema.PKG}.Session/Release", req, "Handle")
        obj.handle_id = None

    async def invoke(self, cls_name, m, handle_id, args):
        from fake_library_generated._runtime import InternalError, WrapperError

        Req = self.msg(f"{cls_name}_{m['name']}Req")
        RespName = _resp(cls_name, m)
        req = Req()
        req.self.id = handle_id

        for p, val in zip(m["params"], args):
            field = getattr(req, p["name"])
            t = p["type"]
            if t == "StorePath":
                field.base_name = val.to_string()          # sync value -> msg
            elif t == "DerivedPath":
                base, out = val._parts()
                field.path.base_name = base
                if out:
                    field.output = out
            elif t in ("Value", "Derivation"):
                field.id = val.handle_id                    # proxy arg = handle
            else:
                setattr(req, p["name"], val)

        try:
            resp = await self._rpc(
                f"/{schema.PKG}.{cls_name}Service/{m['name']}", req, RespName)
        except grpclib.exceptions.GRPCError as e:
            # Typed wrapper errors cross as JSON in the status message.
            # No `from` clause: the rebuilt error keeps the decoded
            # cause as __cause__; the GRPCError stays visible as
            # __context__.
            try:
                d = json.loads(e.message)
                if isinstance(d, dict) and "code" in d:
                    raise WrapperError.from_dict(d)
            except (ValueError, TypeError):
                pass
            raise

        rt = m["return_type"]
        if rt in ("Value", "Derivation"):
            return RemoteObj(self, rt, resp.result.id)      # proxy stays remote
        if rt == "StorePath":
            import fake_library as fl
            return fl.StorePath._from_base_name(resp.result.base_name)
        if rt == "DerivedPath":
            import fake_library as fl
            return fl.DerivedPath._from_parts(
                fl.StorePath._from_base_name(resp.result.path.base_name),
                resp.result.output or None)
        if rt == "None":
            return None
        return getattr(resp, "result")                      # scalars


def _resp(cls_name, m):
    return f"{cls_name}_{m['name'].title().replace('_', '')}Resp"


async def connect(host="127.0.0.1", port=50051) -> NixClient:
    return NixClient(host, port)
