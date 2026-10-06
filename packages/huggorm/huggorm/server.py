"""
The server: a spec-driven adapter onto huggorm_generated, on a Unix
socket.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _copied
semantics), awaits the method on the wrapper's runner, encodes the
result. Handlers are built in a loop from the emitted specs; nothing is
hand-written per method.

`protocol` holds the frames. One connection is one token: the HELLO
binds it, and every call on the connection runs under it.
"""

from __future__ import annotations

import contextlib
import enum
import functools
import logging
import os
import weakref
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import SocketStream

from huggorm_generated._callspec import Acquire, Call, Entries, Items, Leaf, Tree
from huggorm_generated._policy import (
    ACQUIRE,
    ASYNC_CLASS,
    CALLS,
    FREE,
    METHODS,
    TREES,
)

from . import tree
from .codec import Codec
from .lifecycle import HandleTable, ShareMode
from .logbus import Share, widest
from .protocol import (
    Channel,
    Control,
    Faults,
    Op,
    ProtocolError,
    Refusal,
    Refused,
    build_identity,
    peer_uid,
)

logger = logging.getLogger(__name__)

# How long a new connection has to say HELLO.
HELLO_TIMEOUT = 10.0


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
        # asks for the server's default.
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

    Duck-typed as an `AsyncLogStream` on purpose: a remote
    `subscribe_logs` answers one as a `LogStream` handle, and the
    client's `drain`, `dropped` and `close` calls reach it unchanged.
    """

    def __init__(self, fan: _Fanout, capacity: int, level: int) -> None:
        super().__init__(capacity, level)
        self._fan = fan

    async def drain(self) -> list[Any]:
        # The shared drain raised, so the queue this reads is not
        # being filled any more. Re-raised HERE, so the client
        # learns. A reader that just went quiet would not say so.
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
    def __init__(self, tasks: Any, loops: Any,
                 lease_ttl: float = 120.0,
                 escrow_ttl: float | None = 300.0) -> None:
        """Every table it reads is emitted, in
        `huggorm_generated._policy`, so this reads them by name.

        The handlers are built in a loop and that is right: the
        body of one is a RULE - decode, call, encode - and it reads
        the same for every method. What differs is the spec, and the
        build writes that."""
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
        self.codec = Codec()
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
        # A failure crosses the same way a value does: by what the
        # bindings declare, never by a type this file names
        # (huggorm#36).
        self.faults = Faults(self.codec)
        self.handlers = self._handlers()

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

    @staticmethod
    def _failure(e: Exception, label: str) -> Exception:
        """The error a failed call answers.

        A declared error crosses as itself, so the far side rebuilds
        the class rather than approximating it by name. Anything else
        crosses as an InternalError that carries it, so unknown handles
        and bugs arrive debuggable, not anonymous (huggorm#36,
        huggorm#66).

        The test is `to_dict`, not `isinstance(e, WrapperError)`. A
        declared Nix error cannot subclass WrapperError, because the
        bindings are imported BY the generated runtime and cannot
        import it back. The runtime applies the same duck-type one
        layer down."""
        from huggorm_generated._runtime import InternalError

        if hasattr(e, "to_dict") or isinstance(e, Refused):
            return e
        return InternalError(f"{label} failed", cause=e)

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

    def _arguments(self, spec: Call | Acquire, raw: list[Any],
                   resolve: Callable[[str], Any]) -> list[Any]:
        """A call's decoded arguments. A constructor may be sent fewer
        than it declares, and the binding fills in the defaults."""
        least = spec.required if isinstance(spec, Acquire) else len(spec.args)
        if not least <= len(raw) <= len(spec.args):
            raise TypeError(
                f"{len(raw)} argument(s) for {least}..{len(spec.args)}")
        return [self.codec.decode(a.type, v, resolve)
                for a, v in zip(spec.args, raw, strict=False)]

    async def _run_call(self, token: str, index: int, hid: Any,
                        raw: list[Any]) -> Any:
        """One CALL, answered as a codec value.

        A proxy answer leases to the caller's connection. A method's
        answer also pins the handle it ran on (parents=[hid]), so a
        value read out of a state keeps that state alive."""
        if not 0 <= index < len(CALLS):
            raise ProtocolError(f"no call numbered {index}")
        spec, h = CALLS[index], self.handlers[index]
        returns = spec.returns if isinstance(spec, Call) else None

        def resolve(handle: str) -> Any:
            return self.resolve(handle, token)

        match h.target:
            case Target.HANDLE:
                if type(hid) is not str:
                    raise ProtocolError(f"{h.label} names no handle")
                target = resolve(hid)
                result = await h.run(token, target,
                                     *self._arguments(spec, raw, resolve))
                return self.codec.encode(
                    returns, result,
                    lambda obj: self.put(obj, token, parents=[hid]))
            case Target.NEW:
                return self.put(
                    await h.run(token, *self._arguments(spec, raw, resolve)),
                    token)
            case Target.NONE:
                result = await h.run(token,
                                     *self._arguments(spec, raw, resolve))
                return self.codec.encode(returns, result,
                                         lambda obj: self.put(obj, token))

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

    async def logs(self, token: str, hid: str | None, capacity: int,
                   level: int) -> str:
        """A log reader's handle: on the state `hid` names, or on the
        process sink when it names none.

        Not `subscribe_logs`: a reader opened here is outside the set
        a remote `unsubscribe_logs` leaves, so a caller reading a
        state's log keeps reading through a subscribe and an
        unsubscribe on the same state (huggorm#85). Releasing the
        handle leaves the fan-out. The reader pins the state it
        reads."""
        if hid is None:
            reader = await self._process_fanout.join(capacity, level)
            return self.put(reader, token)
        fan = self._fanout(self.resolve(hid, token))
        return self.put(await fan.join(capacity, level), token,
                        parents=[hid])

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

    async def _control(self, token: str, control: Control,
                       args: list[Any]) -> Any:
        """One session operation, answered as a codec value."""
        match control, args:
            case Control.PING, []:
                return self.ping(token)
            case Control.SHARE, [str(to_token), str(hid), str(mode)]:
                self.share(token, to_token, hid, ShareMode(mode))
                return None
            case Control.DETACH, [str() | None as hid, bool(all_)]:
                return self.detach(token, hid, all_)
            case Control.RELEASE, [str(hid)]:
                self.release(token, hid)
                return None
            case Control.REALIZE, [str(hid), int(depth), int(budget)]:
                target, root, _walk = await self.realize(token, hid, depth,
                                                         budget)
                # Every node the walk stopped at leases to the caller
                # and pins the root, exactly as a proxy return does.
                return self.codec.encode_tree(
                    root, lambda _cls, obj: self.put(
                        self.adopt(obj, target), token, parents=[hid]))
            case Control.LOGS_BARRIER, [str(hid)]:
                return await self.logs_barrier(token, hid)
            case Control.LOGS, [str() | None as hid, int(capacity), int(level)]:
                return await self.logs(token, hid, capacity, level)
        raise ProtocolError(f"{control.name} with arguments {args!r:.80}")

    # -- connections -----------------------------------------------------
    async def connection(self, stream: SocketStream) -> None:
        """Serve one connection until the peer closes it.

        Never raises: one broken peer must not stop the listener. EOF
        cancels the connection's running calls, because nobody can
        read their answers. It releases nothing: the leases stay
        until the sweeper finds the token silent, or until a client
        that detached claims them back."""
        try:
            await self._connection(stream)
        except Exception:
            logger.warning("a connection failed", exc_info=True)
        finally:
            with anyio.CancelScope(shield=True):
                await stream.aclose()

    async def _connection(self, stream: SocketStream) -> None:
        uid = peer_uid(stream)
        if uid != os.geteuid():
            logger.warning("refused a connection from uid %d", uid)
            return
        channel = Channel(stream)
        with anyio.fail_after(HELLO_TIMEOUT):
            hello = await channel.receive()
        match hello:
            case [Op.HELLO, str(identity), str() | None as claim]:
                pass
            case _:
                raise ProtocolError(f"a connection opened with {hello!r:.80}")
        if identity != build_identity():
            await channel.send([Op.FAULT, None, self.faults.encode(Refused(
                Refusal.IDENTITY,
                f"the client runs build {identity[:12]} and this server "
                f"runs {build_identity()[:12]}: connect with the server's "
                f"huggorm"))])
            return
        token, ttl = self.bind(claim)
        await channel.send([Op.WELCOME, token, ttl])
        running: dict[int, anyio.CancelScope] = {}
        async with anyio.create_task_group() as calls:
            try:
                await self._frames(channel, token, calls, running)
            finally:
                calls.cancel_scope.cancel()

    async def _frames(self, channel: Channel, token: str, calls: Any,
                      running: dict[int, anyio.CancelScope]) -> None:
        """Read frames until EOF. Each CALL and CONTROL runs in its own
        task, so a slow call does not hold up the next."""
        while True:
            try:
                frame = await channel.receive()
            except (anyio.EndOfStream, anyio.BrokenResourceError):
                return
            match frame:
                case [Op.CALL, int(cid), int(index), hid, list(args)]:
                    label = (self.handlers[index].label
                             if index in self.handlers else f"call {index}")
                    calls.start_soon(
                        self._answer, channel, token, cid, running, label,
                        functools.partial(self._run_call, token, index, hid,
                                          args))
                case [Op.CONTROL, int(cid), int(number), list(args)]:
                    try:
                        control = Control(number)
                    except ValueError:
                        raise ProtocolError(
                            f"no control numbered {number}") from None
                    calls.start_soon(
                        self._answer, channel, token, cid, running,
                        f"Session/{control.name.lower()}",
                        functools.partial(self._control, token, control,
                                          args),
                        control is Control.PING)
                case [Op.CANCEL, int(cid)]:
                    scope = running.get(cid)
                    if scope is not None:
                        scope.cancel()
                case [Op.DROP, list(hids)] if all(type(h) is str
                                                  for h in hids):
                    self.release_many(token, hids)
                case _:
                    raise ProtocolError(f"a frame arrived as {frame!r:.80}")

    async def _answer(self, channel: Channel, token: str, cid: int,
                      running: dict[int, anyio.CancelScope], label: str,
                      run: Callable[[], Awaitable[Any]],
                      after_sweep: bool = False) -> None:
        """Run one call and send its RESULT or FAULT. A CANCEL ends it
        with no answer.

        A swept connection's token is gone, and every lease with it.
        Every call but a ping is refused then: a ping answers False,
        which is how the client learns."""
        with anyio.CancelScope() as scope:
            running[cid] = scope
            try:
                if not after_sweep and token not in self.table.connections:
                    raise Refused(
                        Refusal.SWEPT,
                        f"connection {token[:8]} was swept, and its handles "
                        f"with it")
                frame = [Op.RESULT, cid, await run()]
            except Exception as e:
                frame = [Op.FAULT, cid,
                         self.faults.encode(self._failure(e, label))]
            finally:
                running.pop(cid, None)
            with contextlib.suppress(anyio.BrokenResourceError,
                                     anyio.ClosedResourceError):
                await channel.send(frame)


async def serve(path: str, lease_ttl: float = 120.0, *,
                escrow_ttl: float | None = 300.0,
                task_status: Any = anyio.TASK_STATUS_IGNORED) -> None:
    """The server on the Unix socket at `path`, and the two scopes every
    background task lives in.

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
    not cancel its children on exit, it waits for them.

    The socket is mode 0600, and a connection checks the peer's uid
    as well: only this uid may connect."""
    # One `with`, two groups, and the ORDER inside it is the whole
    # point: `loops` is entered second, so it exits first. Ruff asks
    # for the combined form and it says the same thing.
    async with (anyio.create_task_group() as work,
                anyio.create_task_group() as loops):
        dispatcher = Dispatcher(work, loops, lease_ttl=lease_ttl,
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

        listener = await anyio.create_unix_listener(path, mode=0o600)
        logger.info("listening on %s (lease ttl: %s)", path,
                    lease_ttl if lease_ttl else "off")
        task_status.started()
        try:
            await listener.serve(dispatcher.connection)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(path)
            loops.cancel_scope.cancel()


def main(argv: list[str] | None = None) -> None:
    """`python -m huggorm.server PATH [TTL] [--option NAME VALUE]...`

    `--option` sets a process setting as `nix --option` does. The
    server reads no nix.conf: the caller says what differs."""
    import argparse

    from huggorm_bindings import set_setting

    parser = argparse.ArgumentParser(prog="huggorm.server")
    parser.add_argument("path")
    parser.add_argument("ttl", nargs="?", type=float, default=120.0)
    parser.add_argument("--option", nargs=2, action="append", default=[],
                        metavar=("NAME", "VALUE"))
    args = parser.parse_args(argv)
    for name, value in args.option:
        set_setting(name, value)
    anyio.run(serve, args.path, args.ttl)


if __name__ == "__main__":
    main()
