"""
grpclib server: a spec-driven adapter onto huggorm_generated.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _wire
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are built in a loop from the emitted specs; nothing is
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

from huggorm_generated._callspec import Acquire, Call, Tree
from huggorm_generated._policy import (
    ACQUIRE,
    ASYNC_CLASS,
    FREE,
    METHODS,
    TREES,
)

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

# How long a log reader waits when the queue answered nothing. A drain
# is a mutex and a move, so polling costs almost nothing - and the
# alternative is a condition variable in C++, which would buy latency
# and cost a hand-written wait (tasks/032). After a NON-empty drain the
# loop reads again at once, so a burst leaves at full speed and only a
# quiet stream pays this.
LOG_POLL = 0.05


class TreeWalk:
    """One pass over a value that holds values, on that value's OWN
    thread.

    Everything it knows about the type comes from `spec`, which the
    binding declares and the build emits: which accessor says what
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

    def __init__(self, spec: Tree, depth: int, budget: int) -> None:
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
        how = self.spec.identity
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
        kind = getattr(obj, self.spec.kind)()
        scalar = self.spec.scalars.get(kind)
        if scalar is not None:
            type_str, reader = scalar
            return ("scalar", type_str, getattr(obj, reader)())
        if kind == "list":
            how = self.spec.list
            size = getattr(obj, how.size)()
            item = getattr(obj, how.value)
            return ("list", [self.node(item(i), depth + 1) for i in range(size)])
        if kind == "attrs":
            how = self.spec.attrs
            size = getattr(obj, how.size)()
            name, value = getattr(obj, how.name), getattr(obj, how.value)
            return ("attrs", {name(i): self.node(value(i), depth + 1)
                              for i in range(size)})
        # A kind nothing describes: it stays where it is.
        self.truncated = True
        return ("proxy", type(obj).__name__, obj)


def _never_a_proxy(obj: Any) -> str:
    """A proxy id for something the codec promised not to ask about.

    A `LogRecord` is a wire VALUE, so `encode` never reaches the proxy
    arm. Raising here says that rather than handing out a handle
    nothing tracks, which is what a `lambda _: ""` would have done."""
    raise TypeError(
        f"{type(obj).__name__} crossed as a proxy where only wire values "
        f"were expected")


async def _drop_subscription(target: Any) -> None:
    """Clear a state's subscription, on the state's own thread.

    Detached rather than awaited, so the `finally` that calls it needs
    no await of its own - a defence against a cancelled handler that
    two perturbations failed to prove necessary, and that costs
    nothing (tasks/032). Failures go nowhere on purpose: the stream
    this belonged to is already over, and the queue is closed either
    way."""
    with contextlib.suppress(Exception):
        await target.unsubscribe_logs()


async def _drop_process_subscription() -> None:
    """Clear the process-wide subscription. As `_drop_subscription`.

    No target, because there is nothing to name: the sink belongs to
    the process. That is the whole difference between the two, which
    is why they are two three-line functions rather than one with a
    branch."""
    with contextlib.suppress(Exception):
        from huggorm_generated import unsubscribe_process_logs
        await unsubscribe_process_logs()


async def _pump(stream: Any, sub: Any, resp_cls: Any, codec: Any,
                alive: Callable[[], None]) -> None:
    """Drain a queue onto a stream until somebody stops it.

    Both log rpcs run this, and everything they do differently
    happens before it: which queue to drain, and what `alive` means.
    Written once for the reason goal 3 gives - the drop reporting and
    the empty first batch are decisions, and a second copy is a
    second place for one of them to drift.

    An EMPTY first batch, which is the subscription saying it is
    installed. A caller opens the stream to watch work it is about to
    start, and without this it has no way to know when starting is
    safe: the next message would otherwise be the first record, which
    arrives only after the work it was meant to report.

    `alive` is a check with NO side effect, which is the part that
    matters. `table.alive` would refresh the connection, and a log
    stream that kept a connection alive would disable the sweeper for
    as long as it was open. Liveness is the ping loop's job
    (`tasks/049`) and stays there.
    """
    await stream.send_message(resp_cls())
    while True:
        alive()
        records = sub.drain()
        if not records:
            await asyncio.sleep(LOG_POLL)
            continue
        resp = resp_cls()
        codec.encode(resp, "records", "list[LogRecord]", records,
                     _never_a_proxy)
        resp.dropped = sub.dropped()
        await stream.send_message(resp)


class Dispatcher:
    def __init__(self, pool: Any, lease_ttl: float = 120.0) -> None:
        """No manifest. Every table it unpacked is emitted, in
        `huggorm_generated._policy`, so this reads them by name.

        The handlers are still built in a loop and that is right: the
        body of one is a RULE - decode, call, encode - and it reads
        the same for every method. What differs is the spec, and the
        build writes that."""
        self.pool = pool
        self.table = HandleTable(ttl=lease_ttl)
        # Runner-shutdown tasks in flight; see _on_drop.
        self._closing: set[asyncio.Task[None]] = set()
        self.table.on_drop = self._on_drop
        self.codec = WireCodec()
        # Which states already have a log reader. The tap routes by
        # THREAD and a second subscribe REPLACES the first, so two
        # readers on one state would leave the older one silent with
        # nothing said - this repo's named failure mode. Keyed by the
        # wrapper object, because that is what a subscription belongs
        # to; a shared handle leases the same id to two connections
        # and still names one state.
        self._log_readers: dict[int, Any] = {}
        # Whether a process-wide log stream is open. A BOOL, where the
        # per-state readers need a map: there is one sink, so there is
        # nothing to key by.
        self._process_reader = False
        # A failure crosses the same way a value does: as messages, by
        # what the bindings declare, never by a type this file names
        # (tasks/036).
        self.faults = FaultCodec(schema.load_pool())
        self.mapping: dict[str, grpclib.const.Handler] = {}
        self._session()
        # A class with no methods on the wire has no service: it
        # crosses as a value, so the caller already holds the object
        # and calls it locally. METHODS says so by leaving it out.
        for cls_name in METHODS:
            self._service(cls_name)
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

        self._detach(_close())

    def _detach(self, coro: Any) -> None:
        """Run a coroutine to completion with nobody awaiting it.

        RETAINED, for the reason above: asyncio holds a running task
        only weakly. Used where the awaiting code is about to stop
        existing - a dropped handle, and a cancelled log stream whose
        `finally` cannot await anything."""
        task = asyncio.ensure_future(coro)
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
        """Every failure crosses the wire as typed status details:
        errors that can describe themselves as themselves, everything
        else wrapped in InternalError - so unknown handles and bugs
        arrive debuggable, not anonymous.

        An error the bindings DECLARE crosses as its own parts, so the
        far side rebuilds the class rather than approximating it by
        name. That is what makes the remote shape the same as the
        in-process one, for a declared Nix error and for the
        InternalError that carries a genuine bug alike (tasks/036,
        tasks/066).

        The test is `to_dict`, not `isinstance(e, WrapperError)`. It
        is the same duck-type the runtime applies one layer down, and
        for the same reason: a declared Nix error cannot subclass
        WrapperError, because the bindings are imported BY the
        generated runtime and cannot import it back. Catching the
        class here made this layer disagree with the runtime, and the
        answer a caller got then depended on which of the two saw the
        error first."""
        from huggorm_generated._runtime import InternalError

        async def guard(stream: Any) -> None:
            try:
                await handler(stream)
            except Exception as e:
                if hasattr(e, "to_dict"):
                    raise self._fault(e) from e
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
        import huggorm_generated as flg

        name = type(obj).__name__
        cls = ASYNC_CLASS.get(name)
        if cls is None:
            raise TypeError(
                f"{name} has no async wrapper, so it cannot be handed out "
                f"as a handle")
        return getattr(flg, cls)(obj, parent._runner)

    def _service(self, cls_name: str) -> None:
        """One handler per declared method, from the emitted specs.

        A method the wire cannot carry is simply absent from METHODS.
        The generator names it and why at build time, the same as for
        a free function, and the in-process wrapper still has it.

        The handler is ONE function, not one per method. Its body is a
        RULE - decode the arguments, call the method, encode the
        result - and it reads the same for all sixty of them. Emitting
        sixty copies would restate that rule sixty times, which is the
        thing this repo generates code to avoid. What IS per-method is
        the spec, and that is emitted.
        """
        if cls_name in ACQUIRE:
            self._acquire(cls_name)

        for m in METHODS.get(cls_name, ()):
            req_cls = self.msg(m.req)
            resp_cls = self.msg(m.resp)

            async def handler(stream: Any, m: Call = m,
                              resp_cls: Any = resp_cls) -> None:
                req = await stream.recv_message()
                token = _tok(stream)
                target = self.resolve(req.self.id, token)
                args = [
                    self.codec.decode(req, a.name, a.type,
                                      lambda hid: self.resolve(hid, token))
                    for a in m.args]
                result = await getattr(target, m.name)(*args)
                resp = resp_cls()
                # Proxy returns pin their producer (parents=[self]) and
                # lease to the CALLER's connection; everything else the
                # codec serializes by declared type.
                self.codec.encode(
                    resp, "result", m.returns, result,
                    lambda obj: self.put(obj, token, parents=[req.self.id]))
                await stream.send_message(resp)

            self.mapping[m.path] = grpclib.const.Handler(
                self._wrap(handler, f"{cls_name}.{m.name}"),
                grpclib.const.Cardinality.UNARY_UNARY, req_cls, resp_cls)

    def _acquire(self, cls_name: str) -> None:
        """Construct one instance, from typed constructor arguments.

        The old Session/Acquire took a class NAME and nothing else, so
        it could only build things whose constructor needs no arguments
        - and it decided which those were by inspecting __init__,
        which reports (self, /, *args, **kwargs) for every bound class
        alike. The check was a constant True. Construction now lives on the
        class's own service with its declared parameters."""
        import huggorm_generated as flg

        spec = ACQUIRE[cls_name]
        wrapper_cls = getattr(flg, "Async" + cls_name)
        req_cls = self.msg(spec.req)
        handle_cls = self.msg("Handle")

        async def handler(stream: Any, wrapper_cls: Any = wrapper_cls,
                          spec: Acquire = spec,
                          handle_cls: Any = handle_cls) -> None:
            req = await stream.recv_message()
            token = _tok(stream)
            args = [self.codec.decode(req, a.name, a.type,
                                      lambda hid: self.resolve(hid, token),
                                      optional=a.name in spec.optional)
                    for a in spec.args]
            resp = handle_cls()
            resp.id = self.put(wrapper_cls(*args), token)
            await stream.send_message(resp)

        self.mapping[spec.path] = grpclib.const.Handler(
            self._wrap(handler, f"{cls_name}.Acquire"),
            grpclib.const.Cardinality.UNARY_UNARY, req_cls, handle_cls)

    def _free_service(self) -> None:
        """Module-level functions, on one shared service.

        They have no instance, so their requests carry no `self` handle
        - the only structural difference from a method. Functions whose
        parameters or return type the wire cannot represent are absent
        from the schema; the generator names them and why at build
        time."""
        import huggorm_generated as flg

        for fname, spec in FREE.items():
            fn = getattr(flg, fname)
            req_cls = self.msg(spec.req)
            resp_cls = self.msg(spec.resp)

            async def handler(stream: Any, fn: Any = fn,
                              spec: Call = spec,
                              resp_cls: Any = resp_cls) -> None:
                req = await stream.recv_message()
                token = _tok(stream)
                args = [
                    self.codec.decode(req, a.name, a.type,
                                      lambda hid: self.resolve(hid, token))
                    for a in spec.args]
                result = await fn(*args)
                resp = resp_cls()
                self.codec.encode(resp, "result", spec.returns, result,
                                  lambda obj: self.put(obj, token))
                await stream.send_message(resp)

            self.mapping[spec.path] = grpclib.const.Handler(
                self._wrap(handler, f"Functions.{fname}"),
                grpclib.const.Cardinality.UNARY_UNARY, req_cls, resp_cls)

    def _session(self) -> None:
        from huggorm_generated._runtime import InternalError

        def guard_untyped(fn: Handler) -> Handler:
            """Session rpcs raise plain KeyError/ValueError from the
            lifecycle core; give them the same typed-JSON contract as
            the service handlers."""
            async def guarded(stream: Any) -> None:
                try:
                    await fn(stream)
                except grpclib.exceptions.GRPCError:
                    # Already the wire shape, and already carrying a
                    # status somebody chose. Wrapping it would make a
                    # deliberate refusal - Logs on a state that
                    # already has a reader - read as an internal bug
                    # and lose the code that said which it was.
                    raise
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
            spec = TREES.get(type(target).__name__.removeprefix("Async"))
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

        async def logs(stream: Any) -> None:
            """Records Nix raised, streamed as they arrive.

            The one rpc that travels the other way. Everything else
            here answers a question; this answers records nobody asked
            for one at a time, so it is server-streaming and it is
            HAND-WRITTEN. No binding declares it, because there is no
            method it is the wire form of (tasks/032).

            The subscription belongs to the state's THREAD, so the
            request names an EvalState and the subscribe hops onto
            that state's own thread. Draining does not: `LogStream` is
            pool-threaded and holds its own mutex, so the loop below
            reads it from the event loop while the evaluation it is
            reporting on is still running. That is the whole reason
            the queue is a class rather than a method on EvalState.

            Four things it refuses to do quietly.

            A SECOND reader on one state is refused, not accepted. A
            second subscribe replaces the first in the C++, so
            accepting would leave the older stream open and empty
            forever.

            A DROP is reported. `dropped` rides with every batch and
            is cumulative, so a client that missed a batch still
            learns the total.

            A SWEPT connection ends the stream with a status. A stream
            that just stopped would be indistinguishable from a quiet
            one.

            CLEANUP is synchronous first, which is a cheap defence
            rather than a measured need. The argument was that grpclib
            cancels this task when the client goes away, so the first
            `await` in a `finally` re-raises. Two perturbations say
            otherwise - awaiting before the pop passed, and an
            `asyncio.sleep` before it passed - so cancellation is not
            observed here. The ordering stays because it costs
            nothing and it holds under a second cancel or a deadline,
            where delivered-once is not the whole story (tasks/032)."""
            req = await stream.recv_message()
            token = _tok(stream)
            target = self.resolve(req.state.id, token)
            key = id(target)
            if key in self._log_readers:
                raise grpclib.exceptions.GRPCError(
                    grpclib.const.Status.FAILED_PRECONDITION,
                    f"{req.state.id[:8]} already has a log stream open. A "
                    f"second subscription replaces the first on that "
                    f"state's thread, so the first would go silent "
                    f"without saying so.")
            opts: dict[str, int] = {}
            # Capacity 0 is not a queue, so zero means "the binding's
            # default". Level 0 IS a subscription - lvlError, errors
            # only - so it needs the presence the schema gives it.
            if req.capacity:
                opts["capacity"] = req.capacity
            if req.HasField("level"):
                opts["level"] = req.level
            # CLAIMED before the first await, which is the whole
            # ordering. `subscribe_logs` hops onto the state's thread,
            # so it yields - and with the claim after it, two
            # concurrent requests both passed the check above, both
            # subscribed, and the second replaced the first's queue in
            # the C++. The first stream then sat connected and silent,
            # which is exactly what the refusal exists to prevent.
            #
            # NOT GATED, and deliberately: the failure needs two opens
            # landing inside one thread hop, so a gate for it would
            # assert a schedule rather than a fact. Stated here and in
            # `tasks/085` instead.
            self._log_readers[key] = target
            sub = None

            def alive() -> None:
                if req.state.id not in self.table.entries:
                    raise grpclib.exceptions.GRPCError(
                        grpclib.const.Status.UNAVAILABLE,
                        f"the connection holding {req.state.id[:8]} was "
                        f"swept, so this stream has nothing to read.")

            try:
                sub = await target.subscribe_logs(**opts)
                await _pump(stream, sub, self.msg("LogsResp"), self.codec,
                            alive)
            except grpclib.exceptions.StreamTerminatedError:
                # The client is gone. There is nobody to tell.
                return
            finally:
                self._log_readers.pop(key, None)
                # None when the subscribe itself failed, which
                # installed nothing - so there is nothing to close and
                # nothing to drop.
                if sub is not None:
                    sub.close()
                    self._detach(_drop_subscription(target))

        async def process_logs(stream: Any) -> None:
            """Records no subscribed thread claimed, streamed.

            The same rpc with nothing to name. `Logs` takes a handle
            because the tap routes by THREAD and an EvalState owns
            one; this one takes what a fetcher thread, a
            file-transfer thread or a build raised, and none of those
            belongs to a state (`tasks/085`).

            So it holds NO lease and refreshes nothing. A caller with
            no handle at all can open it, which is right: the records
            it carries are the ones no handle could have reached.

            REFUSES a second stream, where the C++ replaces. That
            split is the answer to the scoping question `tasks/032`
            opened. Replacing in the C++ is what stops an in-process
            caller wedging the sink by dropping its `LogStream`;
            refusing here is what stops one connection silencing
            another's stream, and it cannot wedge, because the
            `finally` below runs when the stream ends.

            ONE reader, not per-connection, and that is the
            limitation to read twice. There is one process-wide queue,
            so the first connection to ask gets every unclaimed
            record in the process and the second is told no. Fan-out
            is what would change that, and it is `tasks/085`'s third
            gap for the per-state stream too."""
            from huggorm_generated import subscribe_process_logs

            req = await stream.recv_message()
            if self._process_reader:
                raise grpclib.exceptions.GRPCError(
                    grpclib.const.Status.FAILED_PRECONDITION,
                    "a process-wide log stream is already open. There is "
                    "one sink, so a second subscription would replace the "
                    "first and leave it connected and silent.")
            opts: dict[str, int] = {}
            if req.capacity:
                opts["capacity"] = req.capacity
            if req.HasField("level"):
                opts["level"] = req.level
            # CLAIMED before the first await. `subscribe_process_logs`
            # goes to the pool, so it yields - and with the claim after
            # it, two concurrent requests both passed the check above
            # and both subscribed, with the second replacing the
            # first's queue in the C++. The first stream would then be
            # connected and silent, which is what the refusal exists
            # to prevent. See `logs` above for why there is no gate.
            self._process_reader = True
            sub = None
            try:
                sub = await subscribe_process_logs(**opts)
                await _pump(stream, sub, self.msg("LogsResp"), self.codec,
                            lambda: None)
            except grpclib.exceptions.StreamTerminatedError:
                return
            finally:
                self._process_reader = False
                if sub is not None:
                    sub.close()
                    self._detach(_drop_process_subscription())

        Handle = self.msg("Handle")
        self.mapping[f"/{schema.PKG}.Session/Logs"] = grpclib.const.Handler(
            guard_untyped(logs), grpclib.const.Cardinality.UNARY_STREAM,
            self.msg("LogsReq"), self.msg("LogsResp"))
        self.mapping[f"/{schema.PKG}.Session/ProcessLogs"] = \
            grpclib.const.Handler(
                guard_untyped(process_logs),
                grpclib.const.Cardinality.UNARY_STREAM,
                self.msg("ProcessLogsReq"), self.msg("LogsResp"))
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
    dispatcher = Dispatcher(pool, lease_ttl=lease_ttl)

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
    # use, so external tools see exactly the generated schema.
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
