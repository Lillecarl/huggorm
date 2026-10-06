"""
grpclib server: a spec-driven adapter onto huggorm_generated.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _copied
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are built in a loop from the emitted specs; nothing is
hand-written per method.
"""

from __future__ import annotations

import contextlib
import enum
import logging
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import anyio
import grpclib
import grpclib.const
import grpclib.exceptions
import grpclib.server
from grpclib.reflection.service import ServerReflection

from huggorm_generated._callspec import Acquire, Call, Entries, Items, Leaf, Tree
from huggorm_generated._policy import (
    ACQUIRE,
    ASYNC_CLASS,
    CALLS,
    FREE,
    LOG_RECORDS,
    METHODS,
    TREES,
)

from . import grpc_pb as schema
from . import tree
from .faults import FaultCodec, SchemaStatusDetails
from .lifecycle import TOKEN_HEADER, HandleTable, ShareMode
from .logbus import LOG_CAPACITY, LOG_LEVEL, Share, widest
from .wire import WireCodec

logger = logging.getLogger(__name__)

# One grpclib handler: it reads the stream and answers on it.
Handler = Callable[[Any], Awaitable[None]]


class Target(enum.Enum):
    """What a call runs on, which decides how its answer is leased."""

    # The handle the request names. A proxy it answers pins that handle.
    HANDLE = enum.auto()
    # Nothing. The answer is a new handle, leased to the caller.
    NEW = enum.auto()
    # Nothing. A proxy it answers pins nothing.
    NONE = enum.auto()


@dataclass(frozen=True, slots=True)
class CallHandler:
    """One call, with no transport in it.

    `run` takes the caller's token, then the resolved target when
    `target` is HANDLE, then the decoded arguments. It answers the
    plain result and raises plain exceptions. The transport decodes,
    leases and encodes around it."""

    target: Target
    run: Callable[..., Awaitable[Any]]
    label: str


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

# A wrapper's declared class, by the wrapper's own name: the emitted
# table read backwards, so no naming convention is restated here.
DECLARED = {async_name: name for name, async_name in ASYNC_CLASS.items()}

# How long a log reader waits when the queue answered nothing. A drain
# is a mutex and a move, so polling costs almost nothing - and the
# alternative is a condition variable in C++, which would buy latency
# and cost a hand-written wait (huggorm#32). After a NON-empty drain the
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

    def node(self, obj: Any, depth: int) -> tree.Node:
        key = self._key(obj)
        # `depth` counts levels EXPANDED, so 1 is the root alone. Zero
        # would be the natural spelling for that, and proto3 cannot
        # tell a zero from an unset field - the same limitation
        # _wire_fields marks with a trailing "?".
        if self.left <= 0 or depth >= self.depth or key in self.seen:
            self.truncated = True
            return tree.Stays(type(obj).__name__, obj)
        self.left -= 1
        self.seen.add(key)
        match self.spec.kinds.get(getattr(obj, self.spec.kind)()):
            case Leaf(wire=wire, read=read):
                return tree.Leaf(wire, getattr(obj, read)())
            case Items(size=size, item=item):
                at = getattr(obj, item)
                return tree.Items([self.node(at(i), depth + 1)
                                   for i in range(getattr(obj, size)())])
            case Entries(size=size, name=name, value=value):
                key, at = getattr(obj, name), getattr(obj, value)
                return tree.Entries({key(i): self.node(at(i), depth + 1)
                                     for i in range(getattr(obj, size)())})
            case None:
                # A kind nothing describes: it stays where it is.
                self.truncated = True
                return tree.Stays(type(obj).__name__, obj)


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
    clears the slot whatever is in it (huggorm#85).

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


class _Reader(Share):
    """One client's share of a subscription many clients read.

    Duck-typed as an `AsyncLogStream` on purpose - `drain` and
    `dropped` are the only two things `_pump` asks of a queue, so a
    reader drops into the same loop the single-reader path used,
    unchanged, awaits included. A remote `subscribe_logs` hands one
    out as a handle for the same reason.
    """

    def __init__(self, fan: _Fanout, capacity: int, level: int) -> None:
        super().__init__(capacity, level)
        self._fan = fan

    async def drain(self) -> list[Any]:
        # Async to match the `AsyncLogStream` this stands in for:
        # `_pump` awaits whichever queue it is given, and a reader
        # that answered synchronously would split that loop in two.
        # The deque moves nothing that waits, so this awaits nothing.
        # The shared drain raised, so the queue this reads is not
        # being filled any more. Re-raised HERE rather than logged,
        # because that is what the single-reader path did: the
        # handler let the failure end the stream, so the client
        # learned. A reader that just went quiet would not say so.
        if self._fan.failure is not None:
            raise self._fan.failure
        return self.take()

    async def dropped(self) -> int:
        """This reader's drops PLUS the shared queue's.

        Both are cumulative, so the sum is too, and a client that
        missed a batch still learns the total. It cannot tell the two
        apart, and does not need to: either way the record is gone.

        Async for the same reason `drain` is: the shape matches.
        """
        return self.own_dropped + self._fan.dropped

    async def close(self) -> None:
        """Leave the fan-out. Idempotent: a reader not in it is a no-op,
        so the reaper and an explicit close can both run."""
        await self._fan.leave(self)

    async def aclose(self) -> None:
        await self.close()


class _Fanout:
    """One subscription in the binding, many readers over it.

    huggorm#85's third gap. The binding REPLACES a subscription, so
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

    def __init__(self, tasks: Any,
                 open_sub: Callable[[int], Awaitable[Any]],
                 drop_sub: Callable[[], Awaitable[None]]) -> None:
        self._tasks = tasks
        self._open = open_sub
        # The level the live subscription was opened at, or None.
        self._level: int | None = None
        self._drop = drop_sub
        self._lock = anyio.Lock()
        self._sub: Any = None
        # The drain's own cancel scope, and the event it sets on the
        # way out. A TASK GROUP hands back no task handle, so this is
        # how `leave` says stop and then waits to be told it stopped -
        # and that ordering is what keeps the unsubscribe after the
        # last drain (huggorm#35).
        self._scope: Any = None
        self._stopped: Any = None
        self._readers: set[_Reader] = set()
        self.dropped = 0
        self.failure: BaseException | None = None

    async def join(self, capacity: int, level: int) -> _Reader:
        """A reader, and the subscription behind it if it is the first.

        A reader that wants MORE than the live subscription reopens
        it. That costs the records in flight during the swap, which a
        joining reader was never going to see anyway - it is the
        price of not subscribing at vomit by default, and huggorm#89
        step 4 says why that default had to go.
        """
        reader = _Reader(self, capacity, level)
        async with self._lock:
            if self._sub is not None and level > (self._level or 0):
                await self._close()
            if self._sub is None:
                self.dropped = 0
                self.failure = None
                self._level = max(level, widest(self._readers))
                self._sub = await self._open(self._level)
                # The subscribe is a call, and its own "finalized"
                # lands in the queue it just installed. No reader asked
                # for that call, so it goes before any reader joins.
                await self._sub.drain()
                self._stopped = anyio.Event()
                # `start`, not `start_soon`: it waits for the task to
                # report its cancel scope, so `leave` can never find
                # `self._scope` still None.
                self._scope = await self._tasks.start(
                    self._run, self._sub, self._stopped)
            self._readers.add(reader)
        return reader

    async def _close(self) -> None:
        """Stop the drain and drop the subscription. LOCK HELD.

        Shared by `leave`, which ends the last reader, and by `join`,
        which reopens at a wider level. Both have to stop the pump
        before the unsubscribe, or the drain outlives the queue it
        reads."""
        scope, stopped, sub = self._scope, self._stopped, self._sub
        self._scope, self._stopped, self._sub = None, None, None
        self._level = None
        if scope is not None:
            scope.cancel()
            await stopped.wait()
        if sub is not None:
            await sub.close()
        with contextlib.suppress(Exception):
            await self._drop()

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
        perturbations in huggorm#32 failed to produce it - so this
        is a defence and not a measured need."""
        self._readers.discard(reader)
        async with self._lock:
            if self._readers or self._sub is None:
                return
            await self._close()

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
                    self.dropped = await sub.dropped()
                    records = await sub.drain()
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


async def _open_process_subscription(level: int) -> Any:
    """Subscribe to the process sink, at the widest level any reader
    wants.

    A function rather than the binding call itself, because the
    import is deferred: `huggorm_generated` is the built extension,
    and this module is imported by tools that never load it."""
    from huggorm_generated import subscribe_process_logs
    return await subscribe_process_logs(level=level)


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
    (huggorm#49) and stays there.
    """
    await stream.send_message(resp_cls())
    while True:
        alive()
        records = await sub.drain()
        if not records:
            await anyio.sleep(LOG_POLL)
            continue
        resp = resp_cls()
        codec.encode(resp, "records", LOG_RECORDS, records, _never_a_proxy)
        resp.dropped = await sub.dropped()
        await stream.send_message(resp)


def _method(name: str) -> Callable[..., Awaitable[Any]]:
    async def run(token: str, target: Any, *args: Any) -> Any:
        return await getattr(target, name)(*args)
    return run


def _construct(wrapper_cls: Any) -> Callable[..., Awaitable[Any]]:
    async def run(token: str, *args: Any) -> Any:
        return wrapper_cls(*args)
    return run


def _function(fn: Any) -> Callable[..., Awaitable[Any]]:
    async def run(token: str, *args: Any) -> Any:
        return await fn(*args)
    return run


class Dispatcher:
    def __init__(self, pool: Any, tasks: Any, loops: Any,
                 lease_ttl: float = 120.0,
                 escrow_ttl: float | None = 300.0) -> None:
        """Every table it reads is emitted, in
        `huggorm_generated._policy`, so this reads them by name.

        The handlers are built in a loop and that is right: the
        body of one is a RULE - decode, call, encode - and it reads
        the same for every method. What differs is the spec, and the
        build writes that."""
        self.pool = pool
        self.rpcs = schema.Rpcs(pool)
        # Two task groups, and which one a task goes in is decided
        # by whether it ENDS. `serve` explains the split; both OWN
        # their children, so nothing here retains a set of tasks by
        # hand (huggorm#35).
        #
        # `tasks` finishes what it holds: a runner shutdown releases
        # an affine thread from the collector's list, and cancelling
        # that is how a dead thread stays registered.
        self.tasks = tasks
        # `loops` is cancelled: a log drain never returns on its own.
        self.loops = loops
        self.table = HandleTable(ttl=lease_ttl, escrow_ttl=escrow_ttl)
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
        # The generated subscribe rpcs, routed through the fan-outs.
        # Called straight through, `subscribe_logs` REPLACES the
        # thread's subscription, and an open `Session/Logs` stream on
        # that state goes connected and silent (huggorm#85). So the
        # handle a remote subscribe answers is a fan-out reader, and a
        # remote unsubscribe leaves only the readers that connection
        # opened. Weak, because the handle table and the fan-out are
        # what keep a reader alive.
        #
        # Keyed by name, never by class: no layer above the bindings
        # names a domain type (`test_no_hardcoded_domain_types`).
        self._method_overrides: dict[str, Callable[..., Awaitable[Any]]] = {
            "subscribe_logs": self._subscribe_logs,
            "unsubscribe_logs": self._unsubscribe_logs,
        }
        self._free_overrides: dict[str, Callable[..., Awaitable[Any]]] = {
            "subscribe_process_logs": self._subscribe_process_logs,
            "unsubscribe_process_logs": self._unsubscribe_process_logs,
        }
        self._handle_readers: dict[tuple[str, int], weakref.WeakSet[_Reader]] = {}
        # A failure crosses the same way a value does: as messages, by
        # what the bindings declare, never by a type this file names
        # (huggorm#36).
        self.faults = FaultCodec(schema.load_pool())
        self.mapping: dict[str, grpclib.const.Handler] = {}
        self._session()
        self.handlers = self._handlers()
        for spec in CALLS:
            self._route(spec.path, self._serve_call(spec, self.handlers[spec.index]))

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
                lambda level: target.subscribe_logs(level=level),
                lambda: _drop_subscription(target))
            self._log_fanouts[key] = fan
        return fan

    async def _join(self, fan: _Fanout, key: tuple[str, int],
                    capacity: int, level: int) -> _Reader:
        reader = await fan.join(capacity, level)
        self._handle_readers.setdefault(key, weakref.WeakSet()).add(reader)
        return reader

    async def _leave_all(self, key: tuple[str, int]) -> None:
        for reader in list(self._handle_readers.pop(key, ())):
            await reader.close()

    async def _subscribe_logs(self, token: str, target: Any,
                              capacity: int, level: int) -> _Reader:
        return await self._join(self._fanout(target), (token, id(target)),
                                capacity, level)

    async def _unsubscribe_logs(self, token: str, target: Any) -> None:
        await self._leave_all((token, id(target)))

    async def _subscribe_process_logs(self, token: str,
                                      capacity: int, level: int) -> _Reader:
        return await self._join(self._process_fanout, (token, 0),
                                capacity, level)

    async def _unsubscribe_process_logs(self, token: str) -> None:
        await self._leave_all((token, 0))

    def _on_drop(self, obj: Any) -> None:
        """Shut a dropped wrapper's runner down, off the sweep.

        The task group OWNS it, which is why nothing retains it here.
        `asyncio.create_task` held only a weak reference, so a
        fire-and-forget task could be collected before it ever ran -
        and this is the path that shuts an affine thread down, which
        is also where that thread leaves the collector's list. Losing
        it silently cost both, and a hand-kept set of tasks was the
        old defence (huggorm#35).

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

    def _route(self, path: str, handler: Handler) -> None:
        """Dispatch `path` to `handler`, with the message types and the
        cardinality the schema states for it."""
        r = self.rpcs[path]
        self.mapping[path] = grpclib.const.Handler(
            handler, r.cardinality, r.req, r.resp)

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
        InternalError that carries a genuine bug alike (huggorm#36,
        huggorm#66).

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
        return getattr(flg, cls)._adopt(obj, parent._runner)

    def _handlers(self) -> dict[int, CallHandler]:
        """Every call's handler, by its index in CALLS.

        Built from the three by-name tables, because only the table
        says whether a `Call` is a method or a free function. CALLS is
        the fourth table, so a call it lists and nothing serves stops
        the server here, before it listens.

        A class with no methods on the wire is absent from METHODS: it
        crosses as a value, so the caller already holds the object and
        calls it locally. The generator names every withheld method
        and function, and why, at build time.

        The handler is ONE rule, not one per method: call the method
        the spec names. Emitting sixty copies would restate that rule
        sixty times, which is the thing this repo generates code to
        avoid. What IS per-method is the spec, and that is emitted."""
        import huggorm_generated as flg

        handlers: dict[int, CallHandler] = {}
        for cls_name, methods in METHODS.items():
            for m in methods:
                handlers[m.index] = CallHandler(
                    Target.HANDLE,
                    self._method_overrides.get(m.name) or _method(m.name),
                    f"{cls_name}.{m.name}")
        for cls_name, acquire in ACQUIRE.items():
            handlers[acquire.index] = CallHandler(
                Target.NEW, _construct(getattr(flg, "Async" + cls_name)),
                f"{cls_name}.Acquire")
        for fname, call in FREE.items():
            handlers[call.index] = CallHandler(
                Target.NONE,
                self._free_overrides.get(fname) or _function(getattr(flg, fname)),
                f"Functions.{fname}")
        listed = {c.index for c in CALLS}
        if handlers.keys() != listed:
            raise RuntimeError(
                f"calls {sorted(listed - handlers.keys())} have no handler, "
                f"and handlers {sorted(handlers.keys() - listed)} answer no "
                f"listed call")
        return handlers

    def _serve_call(self, spec: Call | Acquire, h: CallHandler) -> Handler:
        """One call, over gRPC.

        A proxy answer leases to the CALLER's connection. A method's
        answer also pins the handle it ran on (parents=[self]), so a
        value read out of a state keeps that state alive."""
        resp_cls = self.rpcs[spec.path].resp
        optional = spec.optional if isinstance(spec, Acquire) else ()
        returns = spec.returns if isinstance(spec, Call) else None

        async def handler(stream: Any) -> None:
            req = await stream.recv_message()
            token = _tok(stream)
            target = (self.resolve(req.self.id, token)
                      if h.target is Target.HANDLE else None)
            args = [self.codec.decode(req, a.name, a.type,
                                      lambda hid: self.resolve(hid, token),
                                      optional=a.name in optional)
                    for a in spec.args]
            resp = resp_cls()
            match h.target:
                case Target.HANDLE:
                    result = await h.run(token, target, *args)
                    self.codec.encode(
                        resp, "result", returns, result,
                        lambda obj: self.put(obj, token, parents=[req.self.id]))
                case Target.NEW:
                    resp.id = self.put(await h.run(token, *args), token)
                case Target.NONE:
                    result = await h.run(token, *args)
                    self.codec.encode(resp, "result", returns, result,
                                      lambda obj: self.put(obj, token))
            await stream.send_message(resp)

        return self._wrap(handler, h.label)

    # -- session operations ---------------------------------------------
    def bind(self, claim: str | None) -> tuple[str, float]:
        """A connection token, and the lease TTL it must ping within.
        `claim` takes back the escrow a detached connection left."""
        return self.table.bind(claim), self.table.ttl or 0.0

    def ping(self, token: str) -> bool:
        """False, not an error, for a swept connection: being swept is
        a fact about the connection, not a failure of this call."""
        return self.table.alive(token)

    def share(self, token: str, to_token: str, hid: str,
              mode: ShareMode) -> None:
        self.table.share(token, to_token, hid, mode=mode)

    def detach(self, token: str, hid: str | None, all: bool) -> bool:
        if hid is None and not all:
            raise ValueError("detach needs a target handle or all=true")
        return self.table.detach(token, hid) > 0

    def release(self, token: str, hid: str) -> None:
        self.table.release(token, hid)

    def release_many(self, token: str, hids: Iterable[str]) -> tuple[int, int]:
        """Best-effort batch release for handles the client dropped.

        Per-handle tolerance is the point. The client queues an id
        when its last local reference goes away, and by flush time
        that lease may already be gone - closed explicitly,
        transferred, or swept with an earlier connection. One stale
        id must not cost the caller the rest of the batch, so this
        counts (released, unknown) instead of raising."""
        released = unknown = 0
        for hid in hids:
            try:
                self.table.release(token, hid)
                released += 1
            except (KeyError, ValueError):
                unknown += 1
        return released, unknown

    async def realize(self, token: str, hid: str, depth: int,
                      budget: int) -> tuple[Any, tree.Node, TreeWalk]:
        """One round trip for a whole value tree: the target, the tree,
        and the walk that built it.

        Walking a value from the client is a call per node, and
        every one of them is a network round trip plus a thread
        handover. This walks it once, on the value's own thread.

        It forces nothing. What is already forced serializes; a
        thunk crosses as a handle, and the caller forces it with
        the call that already exists. So the answer is bounded, it
        cannot raise halfway down a half-built message, and it
        composes with force rather than duplicating it."""
        target = self.resolve(hid, token)
        spec = TREES.get(DECLARED.get(type(target).__name__, ""))
        if spec is None:
            raise TypeError(
                f"{hid[:8]} is not a value tree: its type declares no walk")
        walk = TreeWalk(spec, depth if depth > 0 else DEFAULT_DEPTH,
                        budget if budget > 0 else DEFAULT_BUDGET)
        # ONE hop for the whole tree, on the value's own thread.
        root = await target._runner.run(lambda obj: walk.node(obj, 0))
        return target, root, walk

    async def logs_barrier(self, token: str, hid: str) -> int:
        """The request id whose "finalized" ends the records so far.

        One call on the state's own thread. `end_request` pushes
        its marker into that thread's queue after every record the
        thread raised before, so a `Logs` reader that sees it has
        all of them. The process stream has no such order: its
        records come from threads no call owns."""
        from huggorm_bindings import current_request

        target = self.resolve(hid, token)
        request: int = await target._runner.run(lambda _: current_request())
        if not request:
            raise TypeError(
                f"{hid[:8]} runs no call of its own, so no marker can end "
                f"its records")
        return request

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

        def reply(rpc: str) -> Any:
            return self.rpcs[schema.session(rpc)].resp

        async def release_many(stream: Any) -> None:
            req = await stream.recv_message()
            resp = reply("ReleaseMany")()
            resp.released, resp.unknown = self.release_many(
                _tok(stream), (h.id for h in req.handles))
            await stream.send_message(resp)

        async def release(stream: Any) -> None:
            req = await stream.recv_message()
            self.release(_tok(stream),
                         req.self.id if hasattr(req, "self") else req.id)
            await stream.send_message(reply("Release")())

        digest = schema.schema_digest()

        async def bind(stream: Any) -> None:
            req = await stream.recv_message()
            # A client built from another schema numbers fields its own
            # way, and every later call would decode wrongly without an
            # error. Refused here, before it holds anything (huggorm#22).
            if req.schema_digest != digest:
                raise grpclib.exceptions.GRPCError(
                    grpclib.const.Status.FAILED_PRECONDITION,
                    f"client schema {req.schema_digest[:12] or 'none'} is "
                    f"not this server's {digest[:12]}: rebuild the client "
                    f"from the server's huggorm")
            resp = reply("Bind")()
            resp.token, resp.lease_ttl = self.bind(req.claim_token or None)
            await stream.send_message(resp)

        async def ping(stream: Any) -> None:
            await stream.recv_message()
            ack = reply("Ping")()
            ack.ok = self.ping(_tok(stream))
            await stream.send_message(ack)

        async def share(stream: Any) -> None:
            req = await stream.recv_message()
            self.share(_tok(stream), req.to_token, req.handle.id,
                       ShareMode(req.mode or ShareMode.COPY))
            ack = reply("Share")()
            ack.ok = True
            await stream.send_message(ack)

        async def detach(stream: Any) -> None:
            req = await stream.recv_message()
            ack = reply("Detach")()
            ack.ok = self.detach(
                _tok(stream),
                req.target.id if req.HasField("target") else None, req.all)
            await stream.send_message(ack)

        async def realize(stream: Any) -> None:
            req = await stream.recv_message()
            token = _tok(stream)
            target, root, walk = await self.realize(token, req.handle.id,
                                                    req.depth, req.budget)
            resp = reply("Realize")()
            # Every node the walk stopped at leases to the caller and
            # pins the root, exactly as a proxy return does.
            self.codec.tree_to_msg(
                root, resp.root,
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
            method it is the wire form of (huggorm#32).

            The subscription belongs to the state's THREAD, so the
            request names an EvalState and the subscribe hops onto
            that state's own thread. Draining does not: `LogStream` is
            pool-threaded and holds its own mutex, so the loop below
            reads it from the event loop while the evaluation it is
            reporting on is still running. That is the whole reason
            the queue is a class rather than a method on EvalState.

            MANY readers on one state, which is huggorm#85's third
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
                await _pump(stream, reader, reply("Logs"), self.codec, alive)
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
            belongs to a state (huggorm#85).

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
                await _pump(stream, reader, reply("ProcessLogs"), self.codec,
                            lambda: None)
            except grpclib.exceptions.StreamTerminatedError:
                return
            finally:
                await self._process_fanout.leave(reader)

        async def logs_barrier(stream: Any) -> None:
            req = await stream.recv_message()
            resp = reply("LogsBarrier")()
            resp.request = await self.logs_barrier(_tok(stream), req.state.id)
            await stream.send_message(resp)

        handlers: dict[str, Handler] = {
            "Bind": bind, "Ping": ping, "Share": share, "Detach": detach,
            "Release": release, "ReleaseMany": release_many,
            "Realize": realize, "Logs": logs, "ProcessLogs": process_logs,
            "LogsBarrier": logs_barrier,
        }
        # An rpc the schema declares and nothing serves would answer
        # UNIMPLEMENTED at its first call, so the server refuses to start.
        declared = {m.name for m in self.pool.FindServiceByName(  # type: ignore[no-untyped-call]
            f"{schema.PKG}.Session").methods}
        if declared != handlers.keys():
            raise RuntimeError(
                f"Session rpcs {sorted(declared)} and handlers "
                f"{sorted(handlers)} disagree")
        for name, fn in handlers.items():
            self._route(schema.session(name), guard_untyped(fn))

async def serve(host: str = "127.0.0.1", port: int = 50051,
                lease_ttl: float = 120.0, *,
                escrow_ttl: float | None = 300.0) -> None:
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

    That split is the trap huggorm#35 names first: a task group does
    not cancel its children on exit, it waits for them."""
    pool = schema.load_pool()

    # One `with`, two groups, and the ORDER inside it is the whole
    # point: `loops` is entered second, so it exits first. Ruff asks
    # for the combined form and it says the same thing.
    async with (anyio.create_task_group() as work,
                anyio.create_task_group() as loops):
        dispatcher = Dispatcher(pool, work, loops, lease_ttl=lease_ttl,
                                escrow_ttl=escrow_ttl)

        # Connection liveness: transports never report death; the
        # sweeper notices silence past the TTL and releases what
        # the dead connection held (huggorm#2). It also releases
        # escrow nobody claimed in time (huggorm#143).
        ttls = [t for t in (lease_ttl, escrow_ttl) if t]

        async def sweeper() -> None:
            interval = max(0.5, min(min(ttls) / 4, 5))
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

        if ttls:
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
        # grpc_schema.pb and are in no global registry (huggorm#36).
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
