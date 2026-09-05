"""
grpclib server: a spec-driven adapter onto huggorm_generated.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _wire
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are built in a loop from the emitted specs; nothing is
hand-written per method.
"""

import contextlib
import logging
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio
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

    AWAITED, under `_Fanout`'s lock. It was detached until the
    fan-out landed, and the reason it cannot be any more is the
    fan-out itself: the next `join` must not open a subscription
    while this one is still being dropped, because `unsubscribe`
    clears the slot whatever is in it (`tasks/085`).

    Failures go nowhere on purpose: the stream this belonged to is
    already over, and the queue is closed either way."""
    with contextlib.suppress(Exception):
        await target.unsubscribe_logs()


async def _drop_process_subscription() -> None:
    """Clear the process-wide subscription. As `_drop_subscription`,
    awaited for the same reason.

    No target, because there is nothing to name: the sink belongs to
    the process. That is the whole difference between the two, which
    is why they are two three-line functions rather than one with a
    branch."""
    with contextlib.suppress(Exception):
        from huggorm_generated import unsubscribe_process_logs
        await unsubscribe_process_logs()


# The level a shared subscription asks the binding for: lvlVomit, the
# widest there is. Every reader then filters in Python.
#
# The alternative was to let the FIRST reader's level open the queue
# and refuse a later reader that wanted more. That makes the answer
# depend on arrival order - a warnings-only reader arriving first
# would refuse the CLI listener that wants everything, which is the
# reader `tasks/085` exists for. Asking wide costs nothing real,
# because the global `nix::verbosity` filters BEFORE any logger runs
# (`logging.hh:314`), so a level-7 subscription still receives only
# what the process was already willing to raise.
LOG_LEVEL_ALL = 7

# What a reader gets when the request names neither. The binding's own
# defaults, restated here because the shared subscription no longer
# passes them through (`decl/eval.py:1051`).
LOG_CAPACITY = 1024
LOG_LEVEL = 3


class _Reader:
    """One client's share of a subscription many clients read.

    Duck-typed as a `LogStream` on purpose - `drain` and `dropped` are
    the only two things `_pump` asks of a queue, so a reader drops
    into the same loop the single-reader path used, unchanged.

    The DROP POLICY is the C++ queue's, restated over a `deque`
    because a reader is a second bound under the first. A full reader
    refuses a "msg" and a "result" and nothing else, for the reason
    `LogQueue` gives: a dropped stop leaks a node in the reader's
    activity tree that nothing later closes.

    The test names what to DROP, and that is what made `"finalized"`
    safe to add without touching this class. A control event marks the
    end of a call, so a lost one parks a reader waiting for that call.
    Had this listed what to KEEP instead, the C++ guarantee would have
    died here in silence - which is the eighth shape of this repo's
    named failure mode, and it was checked for rather than assumed.

    `level` filters a "msg" only, which is the same rule and the same
    reason.
    """

    def __init__(self, fan: _Fanout, capacity: int, level: int) -> None:
        self._fan = fan
        self._capacity = capacity
        self._level = level
        self._records: list[Any] = []
        self._dropped = 0

    def offer(self, record: Any) -> None:
        """One record from the shared drain. Never awaits."""
        action = record.action()
        if action == "msg" and record.level() > self._level:
            return
        if action in ("msg", "result") and len(self._records) >= self._capacity:
            self._dropped += 1
            return
        self._records.append(record)

    def drain(self) -> list[Any]:
        # The shared drain raised, so the queue this reads is not
        # being filled any more. Re-raised HERE rather than logged,
        # because that is what the single-reader path did: the
        # handler let the failure end the stream, so the client
        # learned. A reader that just went quiet would not say so.
        if self._fan.failure is not None:
            raise self._fan.failure
        out = self._records
        self._records = []
        return out

    def dropped(self) -> int:
        """This reader's drops PLUS the shared queue's.

        Both are cumulative, so the sum is too, and a client that
        missed a batch still learns the total. It cannot tell the two
        apart, and does not need to: either way the record is gone."""
        return self._dropped + self._fan.dropped


class _Fanout:
    """One subscription in the binding, many readers over it.

    `tasks/085`'s third gap. The binding REPLACES a subscription, so
    two `subscribe_logs` calls on one thread leave the first queue
    orphaned - which is why both log rpcs used to refuse a second
    reader. Carl named the reader that makes the refusal wrong:

    > a CLI would want a global listener that prints to stdout/stderr
    > as things happen

    That listener and a client watching one evaluation are both
    legitimate, and neither should silence the other.

    So the subscription is opened ONCE and the fan-out is here, in
    Python. It is not in the C++ for the reason goal 2 gives: a list
    of queues in `LogTap` would be a mapping the declaration cannot
    say, and nothing about a fan-out needs to run on the evaluation
    thread.

    The LOCK covers the two transitions that await: no reader to one,
    and one reader to none. Without it the teardown of the last
    reader races the setup of the next - `unsubscribe` clears the slot
    whatever is in it, so a detached unsubscribe could close a queue
    a newer reader had just installed, leaving that reader connected
    and silent. That is the failure the old refusal existed to
    prevent, one layer down, and it was live in `process_logs` before
    this.
    """

    def __init__(self, tasks: Any, open_sub: Callable[[], Awaitable[Any]],
                 drop_sub: Callable[[], Awaitable[None]]) -> None:
        self._tasks = tasks
        self._open = open_sub
        self._drop = drop_sub
        self._lock = anyio.Lock()
        self._sub: Any = None
        # The drain's own cancel scope, and the event it sets on the
        # way out. A TASK GROUP hands back no task handle, so this is
        # how `leave` says stop and then waits to be told it stopped -
        # and that ordering is what keeps the unsubscribe after the
        # last drain (`tasks/035`).
        self._scope: Any = None
        self._stopped: Any = None
        self._readers: set[_Reader] = set()
        self.dropped = 0
        self.failure: BaseException | None = None

    async def join(self, capacity: int, level: int) -> _Reader:
        """A reader, and the subscription behind it if it is the first."""
        reader = _Reader(self, capacity, level)
        async with self._lock:
            if self._sub is None:
                self.dropped = 0
                self.failure = None
                self._sub = await self._open()
                self._stopped = anyio.Event()
                # `start`, not `start_soon`: it waits for the task to
                # report its cancel scope, so `leave` can never find
                # `self._scope` still None.
                self._scope = await self._tasks.start(
                    self._run, self._sub, self._stopped)
            self._readers.add(reader)
        return reader

    async def leave(self, reader: _Reader) -> None:
        """Drop a reader, and the subscription with the last one.

        The teardown happens UNDER the lock, including the await of
        the unsubscribe. A `join` that arrives mid-teardown waits and
        then opens a fresh subscription, which is the ordering the
        detached cleanup could not give.

        The DISCARD is outside it, and that is the one thing this
        does before it can be cancelled. The single-reader path
        detached its cleanup so that a cancelled handler could not
        skip it; this one awaits, so a cancel between the two would
        leave a reader nothing drains - and a "start" or a "stop"
        bypasses the capacity check by design, so that reader's deque
        would grow without a bound. Discarding first makes a
        cancelled `leave` leave a CONSISTENT state instead: the
        subscription stays installed and the next `join` reuses it.

        Cancellation is not observed in either handler - two
        perturbations in `tasks/032` failed to produce it - so this
        is a defence and not a measured need."""
        self._readers.discard(reader)
        async with self._lock:
            if self._readers or self._sub is None:
                return
            scope, stopped, sub = self._scope, self._stopped, self._sub
            self._scope, self._stopped, self._sub = None, None, None
            if scope is not None:
                scope.cancel()
                await stopped.wait()
            sub.close()
            with contextlib.suppress(Exception):
                await self._drop()

    async def _run(self, sub: Any, stopped: Any, *,
                   task_status: Any = anyio.TASK_STATUS_IGNORED) -> None:
        """Drain the one queue, offer to every reader.

        `sub` and `stopped` are parameters and not `self._sub` and
        `self._stopped`, because `leave` clears both attributes
        before this task notices the cancel. Reading `self._stopped`
        here raised `AttributeError: 'NoneType' object has no
        attribute 'set'` on the first run, and the server died at
        startup - so the parameters are the fix and not a style.

        The readers set is mutated by `join` and `leave` and read
        here, all on one event loop and none of them across an await
        while iterating - so it needs no lock of its own. The lock
        above is for the awaits, not for the set.

        `Exception` and never the cancellation exception: a cancel
        leaves through the scope, which is what `leave` is waiting
        for. Catching it would make `leave` wait for a task that has
        decided not to stop."""
        with anyio.CancelScope() as scope:
            task_status.started(scope)
            try:
                while True:
                    self.dropped = sub.dropped()
                    records = sub.drain()
                    if not records:
                        await anyio.sleep(LOG_POLL)
                        continue
                    for record in records:
                        for reader in self._readers:
                            reader.offer(record)
            except Exception as exc:  # handed to the readers, not swallowed
                self.failure = exc
        # OUTSIDE the scope, so a cancel reaches it too. `set` takes no
        # checkpoint, so nothing can cancel it away.
        stopped.set()


async def _open_process_subscription() -> Any:
    """Subscribe to the process sink, at the widest level.

    A function rather than the binding call itself, because the
    import is deferred: `huggorm_generated` is the built extension,
    and this module is imported by tools that never load it."""
    from huggorm_generated import subscribe_process_logs
    return await subscribe_process_logs(level=LOG_LEVEL_ALL)


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
            await anyio.sleep(LOG_POLL)
            continue
        resp = resp_cls()
        codec.encode(resp, "records", "list[LogRecord]", records,
                     _never_a_proxy)
        resp.dropped = sub.dropped()
        await stream.send_message(resp)


class Dispatcher:
    def __init__(self, pool: Any, tasks: Any, loops: Any,
                 lease_ttl: float = 120.0) -> None:
        """No manifest. Every table it unpacked is emitted, in
        `huggorm_generated._policy`, so this reads them by name.

        The handlers are still built in a loop and that is right: the
        body of one is a RULE - decode, call, encode - and it reads
        the same for every method. What differs is the spec, and the
        build writes that."""
        self.pool = pool
        # Two task groups, and which one a task goes in is decided
        # by whether it ENDS. `serve` explains the split; both OWN
        # their children, so nothing here retains a set of tasks by
        # hand any more (`tasks/035`).
        #
        # `tasks` finishes what it holds: a runner shutdown releases
        # an affine thread from the collector's list, and cancelling
        # that is how a dead thread stays registered.
        self.tasks = tasks
        # `loops` is cancelled: a log drain never returns on its own.
        self.loops = loops
        self.table = HandleTable(ttl=lease_ttl)
        self.table.on_drop = self._on_drop
        self.codec = WireCodec()
        # One fan-out per state, so many readers share one
        # subscription. Keyed by the wrapper object, because that is
        # what a subscription belongs to; a shared handle leases the
        # same id to two connections and still names one state.
        #
        # NEVER removed when the last reader leaves, and that is the
        # point. Removing on empty puts the map entry and the
        # subscription under two different rules again: a `join` that
        # already holds the object would open a subscription nobody
        # can find, and the reader after it would open a SECOND one -
        # which the binding answers by replacing the first. So an
        # empty fan-out stays, holds nothing, and is dropped with the
        # state it belongs to (`_on_drop`).
        self._log_fanouts: dict[int, _Fanout] = {}
        # One fan-out for the process sink. Not a map, because there
        # is one sink and nothing to key by, and not created lazily
        # for the same reason.
        self._process_fanout = _Fanout(loops, _open_process_subscription,
                                       _drop_process_subscription)
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

    def _fanout(self, target: Any) -> _Fanout:
        """This state's fan-out, made on first use.

        `subscribe_logs` hops onto the state's own thread, so the
        binding call is bound to the target here and the fan-out
        never has to name one."""
        key = id(target)
        fan = self._log_fanouts.get(key)
        if fan is None:
            fan = _Fanout(
                self.loops,
                lambda: target.subscribe_logs(level=LOG_LEVEL_ALL),
                lambda: _drop_subscription(target))
            self._log_fanouts[key] = fan
        return fan

    def _on_drop(self, obj: Any) -> None:
        """Shut a dropped wrapper's runner down, off the sweep.

        The task group OWNS it, which is why nothing retains it here.
        `asyncio.create_task` held only a weak reference, so a
        fire-and-forget task could be collected before it ever ran -
        and this is the path that shuts an affine thread down, which
        is also where that thread leaves the collector's list. Losing
        it silently cost both, and a hand-kept set of tasks was the
        old defence (`tasks/035`).

        `start_soon` is a plain method, not a coroutine, so a
        callback the sweep calls synchronously can still use it."""
        async def _close() -> None:
            # Nothing to report a failure to: the connection that owned
            # this handle is already gone.
            with contextlib.suppress(Exception):
                await obj.aclose()

        # The fan-out goes with the state. It is the one removal that
        # cannot race a reader: the wrapper is being dropped, so no
        # request can resolve to it any more.
        self._log_fanouts.pop(id(obj), None)
        self.tasks.start_soon(_close)

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

            MANY readers on one state, which is `tasks/085`'s third
            gap closed. It used to be one: a second subscribe
            REPLACES the first in the C++, so a second reader would
            have left the first connected and empty. The subscription
            is now opened once per state and `_Fanout` hands each
            reader its own view, so the refusal is gone and nothing
            it protected is lost.

            Three things it still refuses to do quietly.

            A DROP is reported. `dropped` rides with every batch and
            is cumulative, so a client that missed a batch still
            learns the total. It now covers this reader's own drops
            as well as the shared queue's.

            A SWEPT connection ends the stream with a status. A stream
            that just stopped would be indistinguishable from a quiet
            one.

            CLEANUP is AWAITED, where the single-reader path detached
            it. That path could afford to: it held the only
            subscription, so nothing was waiting on the drop. A
            fan-out has to know the subscription is gone before it
            opens the next one, and only the await says so."""
            req = await stream.recv_message()
            token = _tok(stream)
            target = self.resolve(req.state.id, token)
            fan = self._fanout(target)
            # Capacity 0 is not a queue, so zero means "the default".
            # Level 0 IS a subscription - lvlError, errors only - so it
            # needs the presence the schema gives it.
            capacity = req.capacity or LOG_CAPACITY
            level = req.level if req.HasField("level") else LOG_LEVEL

            def alive() -> None:
                if req.state.id not in self.table.entries:
                    raise grpclib.exceptions.GRPCError(
                        grpclib.const.Status.UNAVAILABLE,
                        f"the connection holding {req.state.id[:8]} was "
                        f"swept, so this stream has nothing to read.")

            reader = await fan.join(capacity, level)
            try:
                await _pump(stream, reader, self.msg("LogsResp"), self.codec,
                            alive)
            except grpclib.exceptions.StreamTerminatedError:
                # The client is gone. There is nobody to tell.
                return
            finally:
                # AWAITED, where the single-reader path detached its
                # cleanup. The fan-out has to know the subscription is
                # gone before it opens the next one, and only the
                # await says so.
                await fan.leave(reader)

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

            MANY readers over ONE sink, and the fan-out is what makes
            that true. There is one process-wide queue, so every
            connection that asks reads the same subscription through
            its own `_Reader`. This is the reader Carl named - a CLI
            printing everything as it happens - and it no longer
            costs the next connection its view.

            Replacing in the C++ still stops an in-process caller
            wedging the sink by dropping its `LogStream` without
            unsubscribing. What used to sit beside it here was a
            refusal; `_Fanout` replaces that with a refcount."""
            req = await stream.recv_message()
            capacity = req.capacity or LOG_CAPACITY
            level = req.level if req.HasField("level") else LOG_LEVEL

            reader = await self._process_fanout.join(capacity, level)
            try:
                await _pump(stream, reader, self.msg("LogsResp"), self.codec,
                            lambda: None)
            except grpclib.exceptions.StreamTerminatedError:
                return
            finally:
                await self._process_fanout.leave(reader)

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
    """The server, and the two scopes every background task lives in.

    TWO task groups, nested, because the tasks divide into two kinds
    and one exit rule does not fit both.

    `work` is the outer one and it is AWAITED. It holds the runner
    shutdowns `_on_drop` starts, and each of those releases an affine
    thread from the collector's list - a thing that must finish, not
    be cancelled. A task group waiting for its children is exactly
    right here.

    `loops` is the inner one and it is CANCELLED. It holds the
    sweeper and the log drains, which never return on their own, so
    waiting for them would hang the shutdown. Being inner is what
    orders the two: the loops stop first, and anything they started
    is still awaited by `work` afterwards.

    That split is the trap `tasks/035` names first: a task group does
    not cancel its children on exit, it waits for them."""
    pool = schema.load_pool()

    # One `with`, two groups, and the ORDER inside it is the whole
    # point: `loops` is entered second, so it exits first. Ruff asks
    # for the combined form and it says the same thing.
    async with (anyio.create_task_group() as work,
                anyio.create_task_group() as loops):
        dispatcher = Dispatcher(pool, work, loops, lease_ttl=lease_ttl)

        # Connection liveness: transports never report death; the
        # sweeper notices silence past the TTL and releases what
        # the dead connection held (tasks/002).
        async def sweeper() -> None:
            interval = max(0.5, min(lease_ttl / 4 if lease_ttl else 5, 5))
            while True:
                await anyio.sleep(interval)
                dropped = dispatcher.table.sweep()
                if dropped:
                    # One line per sweep, not per handle: a reaped
                    # connection can hold hundreds, and a library
                    # writing hundreds of lines into someone
                    # else's log is the same mistake as writing
                    # them to stdout.
                    logger.info("swept %d handle(s): %s", len(dropped),
                                ", ".join(hid[:8]
                                          for hid in sorted(dropped)))

        if lease_ttl:
            loops.start_soon(sweeper)

        # Reflection serves descriptors out of the same pool the
        # handlers use, so external tools see exactly the
        # generated schema. One servable PER SERVICE: reflection's
        # list_services reports one name per handler object.
        services = []
        grouped: dict[str, dict[str, grpclib.const.Handler]] = {}
        for path, h in dispatcher.mapping.items():
            svc_name = path.split("/")[1]
            grouped.setdefault(svc_name, {})[path] = h
        for subset in grouped.values():
            class Servable:
                def __mapping__(
                    self,
                    _subset: dict[str, grpclib.const.Handler] = subset,
                ) -> dict[str, grpclib.const.Handler]:
                    return _subset
            services.append(Servable())
        # A new name: extend() hands back reflection's own servable
        # type, not the list that went in.
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
            loops.cancel_scope.cancel()


if __name__ == "__main__":
    import sys
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 50051
    ttl = float(sys.argv[3]) if len(sys.argv) > 3 else 120.0
    anyio.run(serve, host, port, ttl)
