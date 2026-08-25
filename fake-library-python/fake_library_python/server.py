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
import json

import grpclib
import grpclib.const
import grpclib.exceptions
import grpclib.server
from google.protobuf import message_factory
from grpclib.reflection.service import ServerReflection

from . import grpc_pb as schema
from .lifecycle import TOKEN_HEADER, HandleTable
from .wire import WireCodec


def _tok(stream) -> str:
    """The connection token presented with this request ('' if none)."""
    md = stream.metadata or {}
    value = md.get(TOKEN_HEADER, "")
    return value.decode() if isinstance(value, bytes) else value


class Dispatcher:
    def __init__(self, pool, manifest, lease_ttl=120.0):
        self.pool = pool
        self.manifest = manifest
        self.table = HandleTable(ttl=lease_ttl)
        self.table.on_drop = self._on_drop
        self.codec = WireCodec(manifest)
        self.classes = _acquire_able(manifest)
        self.mapping = {}
        self._session()
        for group in ("wrappers", "returned_types"):
            for cls_name, proto in manifest[group].items():
                self._service(cls_name, proto)

    @staticmethod
    def _on_drop(obj):
        # Fire-and-forget runner shutdown; sweep runs on the loop.
        async def _close():
            try:
                await obj.aclose()
            except Exception:
                pass
        asyncio.ensure_future(_close())

    def msg(self, name):
        return message_factory.GetMessageClass(
            self.pool.FindMessageTypeByName(f"{schema.PKG}.{name}"))

    # -- handles ---------------------------------------------------------
    def put(self, obj, token, parents=()) -> str:
        return self.table.put(obj, token, parents)

    def get(self, hid: str):
        return self.table.get(hid)

    # -- handler construction ----------------------------------------------
    def _service(self, cls_name, proto):
        from fake_library_generated._runtime import InternalError, WrapperError

        def wrap(errors, method):
            """Every failure crosses the wire as a typed JSON payload in
            the gRPC status message: WrapperErrors as themselves,
            everything else wrapped in InternalError - so unknown
            handles and bugs arrive debuggable, not anonymous."""
            async def guard(stream):
                try:
                    await errors(stream)
                except WrapperError as e:
                    raise grpclib.exceptions.GRPCError(
                        grpclib.const.Status.UNKNOWN,
                        json.dumps(e.to_dict()))
                except Exception as e:
                    internal = InternalError(f"{cls_name}.{method} failed",
                                             cause=e)
                    raise grpclib.exceptions.GRPCError(
                        grpclib.const.Status.UNKNOWN,
                        json.dumps(internal.to_dict()))
            return guard

        for m in proto["methods"]:
            req_cls = self.msg(m["rpc"]["req"])
            resp_cls = self.msg(m["rpc"]["resp"])

            async def handler(stream, m=m, resp_cls=resp_cls):
                req = await stream.recv_message()
                target = self.get(req.self.id)
                args = [self.codec.decode(req, p["name"], p["type"], self.get)
                        for p in m["params"]]
                result = await getattr(target, m["name"])(*args)
                resp = resp_cls()
                # Proxy returns pin their producer (parents=[self]) and
                # lease to the CALLER's connection; everything else is
                # serialized by the manifest-driven codec.
                self.codec.encode(
                    resp, "result", m["return_type"], result,
                    lambda obj: self.put(obj, _tok(stream), parents=[req.self.id]))
                await stream.send_message(resp)

            self.mapping[m["rpc"]["path"]] = grpclib.const.Handler(
                wrap(handler, m["name"]), grpclib.const.Cardinality.UNARY_UNARY,
                req_cls, resp_cls)

    def _session(self):
        from fake_library_generated._runtime import InternalError

        def guard_untyped(fn):
            """Session rpcs raise plain KeyError/ValueError from the
            lifecycle core; give them the same typed-JSON contract as
            the service handlers."""
            async def guarded(stream):
                try:
                    await fn(stream)
                except Exception as e:
                    internal = InternalError(f"Session/{fn.__name__} failed", cause=e)
                    raise grpclib.exceptions.GRPCError(
                        grpclib.const.Status.UNKNOWN,
                        json.dumps(internal.to_dict()))
            return guarded

        async def acquire(stream):
            req = await stream.recv_message()
            try:
                obj = self.classes[getattr(req, "class")]()
            except Exception as e:
                # Unknown class or failed constructor: same typed JSON
                # contract as the service handlers.
                internal = InternalError("Acquire failed", cause=e)
                raise grpclib.exceptions.GRPCError(
                    grpclib.const.Status.UNKNOWN,
                    json.dumps(internal.to_dict()))
            resp = self.msg("Handle")()
            resp.id = self.put(obj, _tok(stream))
            await stream.send_message(resp)

        async def release(stream):
            req = await stream.recv_message()
            self.table.release(_tok(stream),
                               req.self.id if hasattr(req, "self") else req.id)
            await stream.send_message(self.msg("Handle")())

        async def bind(stream):
            req = await stream.recv_message()
            claim = getattr(req, "claim_token") or None
            resp = self.msg("ConnResp")()
            resp.token = self.table.bind(claim)
            resp.lease_ttl = self.table.ttl or 0.0
            await stream.send_message(resp)

        async def ping(stream):
            req = await stream.recv_message()
            self.table._conn_for(getattr(req, "token"))
            ack = self.msg("AckResp")()
            ack.ok = True
            await stream.send_message(ack)

        async def share(stream):
            req = await stream.recv_message()
            self.table.share(_tok(stream), getattr(req, "to_token"),
                             req.handle.id, mode=getattr(req, "mode") or "copy")
            ack = self.msg("AckResp")()
            ack.ok = True
            await stream.send_message(ack)

        async def detach(stream):
            req = await stream.recv_message()
            token = _tok(stream)
            hid = req.target.id if req.HasField("target") else None
            if hid is None and not getattr(req, "all"):
                raise ValueError("detach needs a target handle or all=true")
            moved = self.table.detach(token, hid)
            ack = self.msg("AckResp")()
            ack.ok = moved > 0
            await stream.send_message(ack)

        Handle = self.msg("Handle")
        AcqReq = self.msg("AcquireReq")
        for name, fn, req_cls, resp_cls in (
            ("Acquire", acquire, AcqReq, Handle),
            ("Release", release, Handle, Handle),
            ("Bind", bind, self.msg("BindReq"), self.msg("ConnResp")),
            ("Ping", ping, self.msg("PingReq"), self.msg("AckResp")),
            ("Share", share, self.msg("ShareReq"), self.msg("AckResp")),
            ("Detach", detach, self.msg("DetachReq"), self.msg("AckResp")),
        ):
            self.mapping[f"/{schema.PKG}.Session/{name}"] = grpclib.const.Handler(
                guard_untyped(fn), grpclib.const.Cardinality.UNARY_UNARY,
                req_cls, resp_cls)


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


async def serve(host="127.0.0.1", port=50051, lease_ttl=120.0):
    pool = schema.load_pool()
    dispatcher = Dispatcher(pool, schema.load_manifest(), lease_ttl=lease_ttl)

    # Connection liveness: transports never report death; the sweeper
    # notices silence past the TTL and releases what the dead
    # connection held (tasks/002).
    async def sweeper():
        interval = max(0.5, min(lease_ttl / 4 if lease_ttl else 5, 5))
        while True:
            await asyncio.sleep(interval)
            dropped = dispatcher.table.sweep()
            for hid in dropped:
                print(f"swept handle {hid[:8]}")

    sweep_task = asyncio.create_task(sweeper()) if lease_ttl else None

    # Reflection serves descriptors out of the same pool the handlers
    # use, so external tools see exactly the manifest-built schema.
    # One servable PER SERVICE: reflection's list_services reports one
    # name per handler object.
    services = []
    grouped: dict[str, dict] = {}
    for path, h in dispatcher.mapping.items():
        svc_name = path.split("/")[1]
        grouped.setdefault(svc_name, {})[path] = h
    for subset in grouped.values():
        class Servable:
            def __mapping__(self, _subset=subset):
                return _subset
        services.append(Servable())
    services = ServerReflection.extend(
        services,
        pool=pool,
    )
    server = grpclib.server.Server(services)
    await server.start(host, port)
    print(f"nixmock gRPC server listening on {host}:{port} "
          f"(lease ttl: {lease_ttl if lease_ttl else 'off'})")
    try:
        await server.wait_closed()
    finally:
        if sweep_task is not None:
            sweep_task.cancel()


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 50051
    ttl = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0
    asyncio.run(serve(host, port, lease_ttl=ttl))
