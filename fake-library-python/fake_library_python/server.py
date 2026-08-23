"""
grpclib server: a manifest-driven adapter onto fake_library_generated.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _wire
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are generated in a loop from the manifest; nothing is
hand-written per method.
"""

import asyncio
import inspect

import grpclib
import grpclib.const
import grpclib.server
from google.protobuf import message_factory

from . import grpc_pb as schema


class Dispatcher:
    def __init__(self, pool, manifest):
        self.pool = pool
        self.manifest = manifest
        self.table = {}
        self.classes = _acquire_able(manifest)
        self.mapping = {}
        self._session()
        for group in ("wrappers", "returned_types"):
            for cls_name, proto in manifest[group].items():
                self._service(cls_name, proto)

    def msg(self, name):
        return message_factory.GetMessageClass(
            self.pool.FindMessageTypeByName(f"{schema.PKG}.{name}"))

    # -- handles ---------------------------------------------------------
    def put(self, obj) -> str:
        hid = uuid_hex()
        self.table[hid] = obj
        return hid

    def get(self, hid: str):
        return self.table[hid]

    def drop(self, hid: str) -> None:
        self.table.pop(hid, None)

    # -- wire-value codec -------------------------------------------------
    def decode(self, type_str, msg):
        import fake_library as fl
        if type_str == "StorePath":
            return fl.StorePath._from_base_name(msg.base_name)
        if type_str == "DerivedPath":
            path = fl.StorePath._from_base_name(msg.path.base_name)
            return fl.DerivedPath._from_parts(path, msg.output or None)
        if type_str in ("Value", "Derivation"):
            return self.get(msg.id)
        return {"str": str, "int": int, "bool": bool}[type_str](msg)

    async def encode(self, resp, field, type_str, result):
        if type_str == "str":
            setattr(resp, field, str(result))
        elif type_str == "int":
            setattr(resp, field, int(result))
        elif type_str == "bool":
            setattr(resp, field, bool(result))
        elif type_str == "StorePath":
            getattr(resp, field).base_name = await result.to_string()
        elif type_str == "DerivedPath":
            base, out = result._parts()
            getattr(resp, field).path.base_name = base
            if out:
                getattr(resp, field).output = out
        else:
            raise TypeError(f"cannot encode {type_str}")

    # -- handler construction ----------------------------------------------
    def _service(self, cls_name, proto):
        for m in proto["methods"]:
            req_cls = self.msg(f"{cls_name}_{m['name']}Req")
            resp_cls = self.msg(_resp(cls_name, m))

            async def handler(stream, m=m, req_cls=req_cls, resp_cls=resp_cls,
                              cls_name=cls_name):
                req = await stream.recv_message()
                target = self.get(req.self.id)
                args = [self.decode(p["type"], getattr(req, p["name"]))
                        for p in m["params"]]
                result = await getattr(target, m["name"])(*args)
                resp = resp_cls()
                rt = m["return_type"]
                if rt in ("Value", "Derivation"):
                    resp.result.id = self.put(result)
                elif rt != "None":
                    await self.encode(resp, "result", rt, result)
                await stream.send_message(resp)

            self.mapping[f"/{schema.PKG}.{cls_name}Service/{m['name']}"] = \
                grpclib.const.Handler(
                    handler, grpclib.const.Cardinality.UNARY_UNARY,
                    req_cls, resp_cls)

    def _session(self):
        async def acquire(stream):
            req = await stream.recv_message()
            cls_name = getattr(req, "class")
            resp = self.msg("Handle")()
            resp.id = self.put(self.classes[cls_name]())
            await stream.send_message(resp)

        async def release(stream):
            req = await stream.recv_message()
            self.drop(req.self.id if hasattr(req, "self") else req.id)
            await stream.send_message(self.msg("Handle")())

        Handle = self.msg("Handle")
        AcqReq = self.msg("AcquireReq")
        EmptyHandleResp = Handle
        self.mapping[f"/{schema.PKG}.Session/Acquire"] = grpclib.const.Handler(
            acquire, grpclib.const.Cardinality.UNARY_UNARY, AcqReq, Handle)
        self.mapping[f"/{schema.PKG}.Session/Release"] = grpclib.const.Handler(
            release, grpclib.const.Cardinality.UNARY_UNARY, Handle, EmptyHandleResp)


def _resp(cls_name, m):
    return f"{cls_name}_{m['name'].title().replace('_', '')}Resp"


def uuid_hex() -> str:
    import uuid
    return uuid.uuid4().hex


def _acquire_able(manifest) -> dict:
    """Constructible classes whose constructor takes no required args."""
    import fake_library as fl
    import fake_library_generated as flg

    out = {}
    for cls_name in manifest["wrappers"]:
        try:
            sig = inspect.signature(getattr(fl, cls_name).__init__)
            params = list(sig.parameters.values())[1:]
        except Exception:
            params = []
        required = [p for p in params
                    if p.default is p.empty and p.kind in
                    (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
        if not required:
            out[cls_name] = getattr(flg, "Async" + cls_name)
    return out


async def serve(host="127.0.0.1", port=50051):
    pool = schema.load_pool()
    dispatcher = Dispatcher(pool, schema.load_manifest())

    class DispatchHandler:
        def __mapping__(self):
            return dispatcher.mapping

    server = grpclib.server.Server([DispatchHandler()])
    await server.start(host, port)
    print(f"nixmock gRPC server listening on {host}:{port}")
    await server.wait_closed()
