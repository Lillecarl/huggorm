"""
grpclib server: a manifest-driven adapter onto cythonix_generated.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _wire
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are generated in a loop from the manifest; nothing is
hand-written per method.
"""

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import grpclib
import grpclib.const
import grpclib.exceptions
import grpclib.server
from google.protobuf import message_factory
from grpclib.reflection.service import ServerReflection

from . import grpc_pb as schema
from .faults import FaultCodec, SchemaStatusDetails
from .lifecycle import TOKEN_HEADER, HandleTable
from .wire import WireCodec

# One grpclib handler: it reads the stream and answers on it.
logger = logging.getLogger(__name__)

Handler = Callable[[Any], Awaitable[None]]


def _tok(stream: Any) -> str:
    """The connection token presented with this request ('' if none)."""
    md: dict[str, Any] = stream.metadata or {}
    value = md.get(TOKEN_HEADER, "")
    return value.decode() if isinstance(value, bytes) else value


# A realize with no bounds asked for. Small on purpose: every node the
# walk stops at costs the caller a lease, and a caller that wants more
# can say so.
DEFAULT_DEPTH = 8
DEFAULT_BUDGET = 1000


class TreeWalk:
    """One pass over a value that holds values, on that value's OWN
    thread.

    Everything it knows about the type comes from `spec`, which the
    binding declares and the manifest carries: which accessor says what
    a node is, which accessor reads each scalar kind, and how to reach
    the elements of a list or an attribute set. This class names no
    type and no method.

    It runs in ONE hop for the whole tree. A node per round trip would
    put a thread handover between every attribute, which is the cost
    the affine model exists to avoid paying repeatedly.

    Three things stop it, and all three produce the same answer - a
    proxy:

    - a kind the declaration does not name. That is a thunk, and a
      thunk is exactly what cannot be serialized;
    - a node already visited. Values are immutable and shared freely,
      so without this a diamond is copied twice and a cycle never
      ends. The repeated position still carries a handle, and identity
      mapping means it is the SAME handle - so the sharing survives
      rather than being flattened away;
    - a node past the depth, or one the budget ran out on.
    """

    def __init__(self, spec: dict[str, Any], depth: int, budget: int) -> None:
        self.spec = spec
        self.depth = depth
        self.left = budget
        self.seen: set[Any] = set()
        self.truncated = False

    def _key(self, obj: Any) -> Any:
        """What makes two nodes the same node.

        Declared, because Python identity is not it wherever a binding
        builds a fresh wrapper per access: two wrappers over one object
        differ, and a wrapper that dies hands its id() to the next one -
        which reads as "already seen" and truncates a tree that was
        never visited."""
        how = self.spec.get("identity")
        return getattr(obj, how)() if how else id(obj)

    def node(self, obj: Any, depth: int) -> Any:
        key = self._key(obj)
        # `depth` counts levels EXPANDED, so 1 is the root alone. Zero
        # would be the natural spelling for that, and proto3 cannot
        # tell a zero from an unset field - the same limitation
        # _wire_fields marks with a trailing "?".
        if self.left <= 0 or depth >= self.depth or key in self.seen:
            self.truncated = True
            return ("proxy", type(obj).__name__, obj)
        self.left -= 1
        self.seen.add(key)
        kind = getattr(obj, self.spec["kind"])()
        scalar = self.spec["scalars"].get(kind)
        if scalar is not None:
            type_str, reader = scalar
            return ("scalar", type_str, getattr(obj, reader)())
        if kind == "list":
            how = self.spec["list"]
            size = getattr(obj, how["size"])()
            item = getattr(obj, how["item"])
            return ("list", [self.node(item(i), depth + 1) for i in range(size)])
        if kind == "attrs":
            how = self.spec["attrs"]
            size = getattr(obj, how["size"])()
            name, value = getattr(obj, how["name"]), getattr(obj, how["value"])
            return ("attrs", {name(i): self.node(value(i), depth + 1)
                              for i in range(size)})
        # A kind nothing describes: it stays where it is.
        self.truncated = True
        return ("proxy", type(obj).__name__, obj)


class Dispatcher:
    def __init__(self, pool: Any, manifest: dict[str, Any],
                 lease_ttl: float = 120.0) -> None:
        self.pool = pool
        self.manifest = manifest
        self.table = HandleTable(ttl=lease_ttl)
        # Runner-shutdown tasks in flight; see _on_drop.
        self._closing: set[asyncio.Task[None]] = set()
        self.table.on_drop = self._on_drop
        self.codec = WireCodec(manifest)
        # A failure crosses the same way a value does: as messages, by
        # what the manifest declares, never by a type this file names
        # (tasks/036).
        self.faults = FaultCodec(manifest, schema.load_pool())
        # Which classes are value TREES, and how to walk one. Declared
        # next to the binding; this module names none of them.
        self.trees: dict[str, dict[str, Any]] = {
            name: proto["tree"]
            for group in ("wrappers", "returned_types")
            for name, proto in manifest[group].items()
            if "tree" in proto
        }
        self.async_classes: dict[str, str] = {
            name: proto["async_class"]
            for group in ("wrappers", "returned_types")
            for name, proto in manifest[group].items()
            if "async_class" in proto
        }
        self.mapping: dict[str, grpclib.const.Handler] = {}
        self._session()
        for group in ("wrappers", "returned_types"):
            for cls_name, proto in manifest[group].items():
                # An unwrapped class has no service: it crosses as a
                # value, so the caller already holds the object and
                # calls it locally. The manifest says so by leaving the
                # rpc names off.
                if "service" in proto:
                    self._service(cls_name, proto)
        self._free_service()

    def _on_drop(self, obj: Any) -> None:
        """Shut a dropped wrapper's runner down, off the sweep.

        The task is RETAINED. asyncio holds only a weak reference to a
        running task, so a fire-and-forget one can be collected before
        it ever runs - and this is the path that shuts an affine
        thread down, which is also where that thread leaves the
        collector's list. Losing it silently costs both."""
        async def _close() -> None:
            # Nothing to report a failure to: the connection that owned
            # this handle is already gone.
            with contextlib.suppress(Exception):
                await obj.aclose()

        task = asyncio.ensure_future(_close())
        self._closing.add(task)
        task.add_done_callback(self._closing.discard)

    def msg(self, name: str) -> Any:
        return message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
            self.pool.FindMessageTypeByName(f"{schema.PKG}.{name}"))

    # -- handles ---------------------------------------------------------
    def put(self, obj: Any, token: str,
            parents: Iterable[str] = ()) -> str:
        return self.table.put(obj, token, parents)

    def get(self, hid: str) -> Any:
        return self.table.get(hid)

    def resolve(self, hid: str, token: str) -> Any:
        """The object behind a handle a request named, and a lease on
        it for the connection that named it.

        A handle id is the access capability, so a connection able to
        name one is entitled to use it - and using it is what makes it
        a holder. Two processes can then share one object by passing
        the id between them however they like: the second one calls,
        and the object stays alive for it without the first having to
        arrange anything. The grant is idempotent, so a connection that
        already holds the handle owes no second release."""
        self.table.touch(token, hid)
        return self.table.get(hid)

    def _fault(self, wrapper: Any) -> grpclib.exceptions.GRPCError:
        """One failure, as the status a failed call can carry.

        The message stays human-readable - it is the field a human
        reads in a log - and the structure goes where structure goes,
        in the typed details beside it."""
        return grpclib.exceptions.GRPCError(
            grpclib.const.Status.UNKNOWN,
            wrapper.message,
            self.faults.details(wrapper))

    # -- handler construction ----------------------------------------------
    def _wrap(self, handler: Handler, label: str) -> Handler:
        """Every failure crosses the wire as a typed JSON payload in the
        gRPC status message: WrapperErrors as themselves, everything
        else wrapped in InternalError - so unknown handles and bugs
        arrive debuggable, not anonymous.

        A cause the manifest DECLARES also crosses as its parts, so the
        far side rebuilds the class rather than approximating it by
        name. That is what makes the remote shape the same as the
        in-process one: an InternalError whose __cause__ is the real
        error, colour and all (tasks/036)."""
        from cythonix_generated._runtime import InternalError, WrapperError

        async def guard(stream: Any) -> None:
            try:
                await handler(stream)
            except WrapperError as e:
                raise self._fault(e) from e
            except Exception as e:
                raise self._fault(
                    InternalError(f"{label} failed", cause=e)) from e
        return guard

    def adopt(self, obj: Any, parent: Any) -> Any:
        """The async wrapper for a bare binding object.

        A handle resolves to a wrapper: the server calls methods on one
        and the reaper closes one. A value found INSIDE another value
        arrives as the sync object, so it needs its wrapper before it
        can have a handle - attached to the producer's runner, because
        an affine object may only be touched on the thread it was born
        on."""
        import cythonix_generated as flg

        name = type(obj).__name__
        cls = self.async_classes.get(name)
        if cls is None:
            raise TypeError(
                f"{name} has no async wrapper, so it cannot be handed out "
                f"as a handle")
        return getattr(flg, cls)(obj, parent._runner)

    def _service(self, cls_name: str, proto: dict[str, Any]) -> None:
        if "acquire" in proto:
            self._acquire(cls_name, proto)

        for m in proto["methods"]:
            if "rpc" not in m:
                # No wire representation, so no handler. The generator
                # names it and why at build time, the same as for a
                # free function - and the in-process wrapper still has
                # the method.
                continue
            req_cls = self.msg(m["rpc"]["req"])
            resp_cls = self.msg(m["rpc"]["resp"])

            async def handler(stream: Any, m: dict[str, Any] = m,
                              resp_cls: Any = resp_cls) -> None:
                req = await stream.recv_message()
                token = _tok(stream)
                target = self.resolve(req.self.id, token)
                args = [
                    self.codec.decode(req, p["name"], p["type"],
                                      lambda hid: self.resolve(hid, token))
                    for p in m["params"]]
                result = await getattr(target, m["name"])(*args)
                resp = resp_cls()
                # Proxy returns pin their producer (parents=[self]) and
                # lease to the CALLER's connection; everything else is
                # serialized by the manifest-driven codec.
                self.codec.encode(
                    resp, "result", m["return_type"], result,
                    lambda obj: self.put(obj, token, parents=[req.self.id]))
                await stream.send_message(resp)

            self.mapping[m["rpc"]["path"]] = grpclib.const.Handler(
                self._wrap(handler, f"{cls_name}.{m['name']}"),
                grpclib.const.Cardinality.UNARY_UNARY, req_cls, resp_cls)

    def _acquire(self, cls_name: str, proto: dict[str, Any]) -> None:
        """Construct one instance, from typed constructor arguments.

        The old Session/Acquire took a class NAME and nothing else, so
        it could only build things whose constructor needs no arguments
        - and it decided which those were by inspecting __init__, which
        reports (self, /, *args, **kwargs) for every Cython class alike.
        The check was a constant True. Construction now lives on the
        class's own service with its declared parameters."""
        import cythonix_generated as flg

        wrapper_cls = getattr(flg, "Async" + cls_name)
        req_cls = self.msg(proto["acquire"]["req"])
        handle_cls = self.msg("Handle")

        async def handler(stream: Any, wrapper_cls: Any = wrapper_cls,
                          proto: dict[str, Any] = proto,
                          handle_cls: Any = handle_cls) -> None:
            req = await stream.recv_message()
            token = _tok(stream)
            args = [self.codec.decode(req, p["name"], p["type"],
                                      lambda hid: self.resolve(hid, token),
                                      optional=p["default"] == "None")
                    for p in proto["ctor"]]
            resp = handle_cls()
            resp.id = self.put(wrapper_cls(*args), token)
            await stream.send_message(resp)

        self.mapping[proto["acquire"]["path"]] = grpclib.const.Handler(
            self._wrap(handler, f"{cls_name}.Acquire"),
            grpclib.const.Cardinality.UNARY_UNARY, req_cls, handle_cls)

    def _free_service(self) -> None:
        """Module-level functions, on one shared service.

        They have no instance, so their requests carry no `self` handle
        - the only structural difference from a method. Functions whose
        parameters or return type the wire cannot represent are absent
        from the schema; the generator names them and why at build
        time."""
        import cythonix_generated as flg

        for fname, proto in self.manifest.get("free_functions", {}).items():
            if "rpc" not in proto:
                continue
            fn = getattr(flg, fname)
            req_cls = self.msg(proto["rpc"]["req"])
            resp_cls = self.msg(proto["rpc"]["resp"])

            async def handler(stream: Any, fn: Any = fn,
                              proto: dict[str, Any] = proto,
                              resp_cls: Any = resp_cls) -> None:
                req = await stream.recv_message()
                token = _tok(stream)
                args = [
                    self.codec.decode(req, p["name"], p["type"],
                                      lambda hid: self.resolve(hid, token))
                    for p in proto["params"]]
                result = await fn(*args)
                resp = resp_cls()
                self.codec.encode(resp, "result", proto["return_type"], result,
                                  lambda obj: self.put(obj, token))
                await stream.send_message(resp)

            self.mapping[proto["rpc"]["path"]] = grpclib.const.Handler(
                self._wrap(handler, f"Functions.{fname}"),
                grpclib.const.Cardinality.UNARY_UNARY, req_cls, resp_cls)

    def _session(self) -> None:
        from cythonix_generated._runtime import InternalError

        def guard_untyped(fn: Handler) -> Handler:
            """Session rpcs raise plain KeyError/ValueError from the
            lifecycle core; give them the same typed-JSON contract as
            the service handlers."""
            async def guarded(stream: Any) -> None:
                try:
                    await fn(stream)
                except Exception as e:
                    raise self._fault(InternalError(
                        f"Session/{fn.__name__} failed", cause=e)) from e
            return guarded

        async def release_many(stream: Any) -> None:
            """Best-effort batch release for handles the client dropped.

            Per-handle tolerance is the point. The client queues an id
            when its last local reference goes away, and by flush time
            that lease may already be gone - closed explicitly,
            transferred, or swept with an earlier connection. One stale
            id must not cost the caller the rest of the batch, so the
            reply counts instead of raising."""
            req = await stream.recv_message()
            token = _tok(stream)
            released = unknown = 0
            for h in req.handles:
                try:
                    self.table.release(token, h.id)
                    released += 1
                except (KeyError, ValueError):
                    unknown += 1
            resp = self.msg("ReleaseManyResp")()
            resp.released, resp.unknown = released, unknown
            await stream.send_message(resp)

        async def release(stream: Any) -> None:
            req = await stream.recv_message()
            self.table.release(_tok(stream),
                               req.self.id if hasattr(req, "self") else req.id)
            await stream.send_message(self.msg("Handle")())

        async def bind(stream: Any) -> None:
            req = await stream.recv_message()
            claim = req.claim_token or None
            resp = self.msg("ConnResp")()
            resp.token = self.table.bind(claim)
            resp.lease_ttl = self.table.ttl or 0.0
            await stream.send_message(resp)

        async def ping(stream: Any) -> None:
            await stream.recv_message()
            ack = self.msg("AckResp")()
            # `ok` finally means something. It was always True, which
            # is why nobody noticed that the lookup behind it CREATED
            # the connection it was meant to be checking.
            #
            # False rather than an error: being swept is a fact about
            # the connection, not a failure of this call, and a
            # liveness probe answering a boolean is the honest shape.
            #
            # The token comes from the metadata, like every other rpc.
            # It used to ride in the request body, which was a second
            # channel for the one thing lifecycle documents a
            # convention for.
            ack.ok = self.table.alive(_tok(stream))
            await stream.send_message(ack)

        async def share(stream: Any) -> None:
            req = await stream.recv_message()
            self.table.share(_tok(stream), req.to_token,
                             req.handle.id, mode=req.mode or "copy")
            ack = self.msg("AckResp")()
            ack.ok = True
            await stream.send_message(ack)

        async def detach(stream: Any) -> None:
            req = await stream.recv_message()
            token = _tok(stream)
            hid = req.target.id if req.HasField("target") else None
            if hid is None and not req.all:
                raise ValueError("detach needs a target handle or all=true")
            moved = self.table.detach(token, hid)
            ack = self.msg("AckResp")()
            ack.ok = moved > 0
            await stream.send_message(ack)

        async def realize(stream: Any) -> None:
            """One round trip for a whole value tree.

            Walking a value from the client is a call per node, and
            every one of them is a network round trip plus a thread
            handover. This walks it once, on the value's own thread,
            and answers with the tree.

            It forces nothing. What is already forced serializes; a
            thunk crosses as a handle, and the caller forces it with
            the call that already exists. So the answer is bounded, it
            cannot raise halfway down a half-built message, and it
            composes with force rather than duplicating it."""
            req = await stream.recv_message()
            token = _tok(stream)
            target = self.resolve(req.handle.id, token)
            spec = self.trees.get(type(target).__name__.removeprefix("Async"))
            if spec is None:
                raise TypeError(
                    f"{req.handle.id[:8]} is not a value tree: its type "
                    f"declares no walk")
            walk = TreeWalk(spec,
                            req.depth if req.depth > 0 else DEFAULT_DEPTH,
                            req.budget if req.budget > 0 else DEFAULT_BUDGET)
            # ONE hop for the whole tree, on the value's own thread.
            tree = await target._runner.run(lambda obj: walk.node(obj, 0))
            resp = self.msg("RealizeResp")()
            # Every node the walk stopped at leases to the caller and
            # pins the root, exactly as a proxy return does.
            self.codec.tree_to_msg(
                tree, resp.root,
                lambda _cls, obj: self.put(self.adopt(obj, target), token,
                                           parents=[req.handle.id]))
            resp.nodes = len(walk.seen)
            resp.truncated = walk.truncated
            await stream.send_message(resp)

        Handle = self.msg("Handle")
        for name, fn, req_cls, resp_cls in (
            ("Realize", realize, self.msg("RealizeReq"),
             self.msg("RealizeResp")),
            ("Release", release, Handle, Handle),
            ("ReleaseMany", release_many, self.msg("ReleaseManyReq"),
             self.msg("ReleaseManyResp")),
            ("Bind", bind, self.msg("BindReq"), self.msg("ConnResp")),
            ("Ping", ping, self.msg("PingReq"), self.msg("AckResp")),
            ("Share", share, self.msg("ShareReq"), self.msg("AckResp")),
            ("Detach", detach, self.msg("DetachReq"), self.msg("AckResp")),
        ):
            self.mapping[f"/{schema.PKG}.Session/{name}"] = grpclib.const.Handler(
                guard_untyped(fn), grpclib.const.Cardinality.UNARY_UNARY,
                req_cls, resp_cls)


async def serve(host: str = "127.0.0.1", port: int = 50051,
                lease_ttl: float = 120.0) -> None:
    pool = schema.load_pool()
    dispatcher = Dispatcher(pool, schema.load_manifest(), lease_ttl=lease_ttl)

    # Connection liveness: transports never report death; the sweeper
    # notices silence past the TTL and releases what the dead
    # connection held (tasks/002).
    async def sweeper() -> None:
        interval = max(0.5, min(lease_ttl / 4 if lease_ttl else 5, 5))
        while True:
            await asyncio.sleep(interval)
            dropped = dispatcher.table.sweep()
            if dropped:
                # One line per sweep, not per handle: a reaped
                # connection can hold hundreds, and a library writing
                # hundreds of lines into someone else's log is the
                # same mistake as writing them to stdout.
                logger.info("swept %d handle(s): %s", len(dropped),
                            ", ".join(hid[:8] for hid in sorted(dropped)))

    sweep_task = asyncio.create_task(sweeper()) if lease_ttl else None

    # Reflection serves descriptors out of the same pool the handlers
    # use, so external tools see exactly the manifest-built schema.
    # One servable PER SERVICE: reflection's list_services reports one
    # name per handler object.
    services = []
    grouped: dict[str, dict[str, grpclib.const.Handler]] = {}
    for path, h in dispatcher.mapping.items():
        svc_name = path.split("/")[1]
        grouped.setdefault(svc_name, {})[path] = h
    for subset in grouped.values():
        class Servable:
            def __mapping__(
                self, _subset: dict[str, grpclib.const.Handler] = subset
            ) -> dict[str, grpclib.const.Handler]:
                return _subset
        services.append(Servable())
    # A new name: extend() hands back reflection's own servable type,
    # not the list that went in.
    reflected = ServerReflection.extend(services, pool=pool)
    # Typed failures ride in grpc-status-details-bin, resolved
    # against this pool rather than protobuf's default symbol
    # database - these descriptors were built at import from
    # grpc_schema.pb and are in no global registry (tasks/036).
    server = grpclib.server.Server(
        reflected, status_details_codec=SchemaStatusDetails(pool))
    await server.start(host, port)
    logger.info("listening on %s:%d (lease ttl: %s)", host, port,
                lease_ttl if lease_ttl else "off")
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
