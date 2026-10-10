"""
The server: a spec-driven adapter onto huggorm_generated, on a Unix
socket.

The async wrappers already own threading policy and thread hopping, so
the server does none of that. It resolves handles to wrapper objects,
decodes wire-values into sync bindings (copies - matching _copied
semantics), awaits the method on the wrapper's backend, encodes the
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
import signal
import weakref
from collections.abc import Awaitable, Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import Any

import anyio
from anyio.abc import SocketStream

from huggorm_bindings.errors import NixError
from huggorm_generated._callspec import (
    Accessor,
    Acquire,
    Builds,
    Call,
    Entries,
    Items,
    Leaf,
    Null,
    Subscription,
    Tree,
)
from huggorm_generated._classes import CLASSES
from huggorm_generated._policy import (
    ACQUIRE,
    BUILDERS,
    CALLS,
    FREE,
    METHODS,
    TREES,
)
from huggorm_generated._runtime import BaseRunner
from huggorm_generated.free_functions import FUNCTIONS

from . import tree
from .callbacks import Callbacks
from .codec import Codec, Node
from .lifecycle import HandleTable, ShareMode
from .logbus import Fanout, Reader
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

# A wrapper's declared class: the emitted table read backwards, so no
# naming convention is restated here.
DECLARED = {cls: name for name, cls in CLASSES.items()}


def _runner(obj: Any) -> BaseRunner:
    """The runner of an object the server holds, which is always in
    this process."""
    backend = obj._backend
    assert isinstance(backend, BaseRunner), backend
    return backend


class TreeWalk:
    """One pass over a value that holds values, on that value's OWN
    thread.

    Everything it knows about the type comes from `spec`, which the
    binding declares and the build emits: which accessor says what
    a node is, which accessor reads each scalar kind, and how to reach
    the elements of a list or an attribute set. Each accessor is
    generated code that calls the method, so this class names no type
    and no method.

    It runs in ONE hop for the whole tree. A node per round trip would
    put a thread handover between every attribute, which is the cost
    the affine model exists to avoid paying repeatedly.

    Three things stop it, and all three produce the same answer - a
    proxy:

    - a kind the declaration does not name. That is a thunk, and a
      thunk is exactly what cannot be serialized;
    - a node already visited. Values are immutable and shared freely,
      so without this a diamond is copied twice and a cycle never
      ends. The repeated position carries a handle to the value
      instead of a copy. Two such positions get two handles: each
      stop is a fresh wrapper, and the table keys on the wrapper;
    - a node past the depth, or one the budget ran out on.

    A FORCING walk forces each node first, with the declared `force`.
    A throw keeps that node a proxy: Nix keeps the error in the value,
    so the caller's first read of it raises the same error, and the
    rest of the tree still arrives. An `Entries` node the declared
    `stop` answers true for stays a proxy too: a derivation, which
    forcing would instantiate.
    """

    def __init__(self, spec: Tree[Accessor], depth: int, budget: int,
                 force: bool = False) -> None:
        self.spec = spec
        self.forcing = force and spec.force is not None
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
        return how(obj) if how is not None else id(obj)

    def _stops(self, obj: Any) -> bool:
        """Whether a forcing walk keeps this node a proxy. A throw
        while asking means the same: the read raises it later."""
        stop = self.spec.stop
        if stop is None:
            return False
        try:
            return bool(stop(obj))
        except NixError:
            return True

    def node(self, obj: Any, depth: int) -> tree.Node:
        key = self._key(obj)
        # `depth` counts levels EXPANDED, so 1 is the root alone. Zero
        # asks for the server's default.
        if self.left <= 0 or depth >= self.depth or key in self.seen:
            self.truncated = True
            return tree.Stays(type(obj).__name__, obj)
        self.left -= 1
        self.seen.add(key)
        if self.forcing and (force := self.spec.force) is not None:
            try:
                force(obj)
            except NixError:
                return tree.Stays(type(obj).__name__, obj)
        match self.spec.kinds.get(self.spec.kind(obj)):
            case Leaf(wire=wire, read=read):
                return tree.Leaf(wire, read(obj))
            case Null():
                return tree.Leaf("null", None)
            case Items(size=size, item=item):
                return tree.Items(type(obj).__name__, obj,
                                  [self.node(item(obj, i), depth + 1)
                                   for i in range(size(obj))])
            case Entries(size=size, name=name, value=value):
                if self.forcing and self._stops(obj):
                    return tree.Stays(type(obj).__name__, obj)
                return tree.Entries(type(obj).__name__, obj,
                                    {name(obj, i): self.node(value(obj, i), depth + 1)
                                     for i in range(size(obj))})
            case None:
                # A kind nothing describes: it stays where it is.
                self.truncated = True
                return tree.Stays(type(obj).__name__, obj)


class Build:
    """Python data made into one value, on the state's OWN thread.

    Every method it calls is in `spec`, which the binding declares and
    the build emits, so this class names no type.

    Bottom-up: a Nix list or attribute set is sized when it is made, so
    a child is complete before its parent takes it. `given` holds the
    objects behind the handles the data named, resolved before the hop.

    It answers the tree it built, so a caller reads it with no further
    round trip. The root is always a handle: the caller asked for a
    value, and a bare scalar is not one."""

    def __init__(self, spec: Builds[Accessor], given: dict[str, Any]) -> None:
        self.spec = spec
        self.given = given

    @staticmethod
    def handles(raw: Any) -> list[str]:
        """Every handle id the data names."""
        match raw:
            case [Node.STAYS, str(hid)]:
                return [hid]
            case [Node.ITEMS, list(items)]:
                return [h for i in items for h in Build.handles(i)]
            case [Node.ENTRIES, dict(entries)]:
                return [h for v in entries.values() for h in Build.handles(v)]
        return []

    def root(self, state: Any, raw: Any) -> tree.Node:
        node, made = self.node(state, raw)
        if isinstance(node, tree.Leaf):
            return tree.Stays(type(made).__name__, made)
        return node

    def node(self, state: Any, raw: Any) -> tuple[tree.Node, Any]:
        """One data node, and the value made for it."""
        match raw:
            case [Node.LEAF, None]:
                made = self.spec.null(state)
                return tree.Leaf("null", None), made
            case [Node.LEAF, bool() | int() | float() | str() as v]:
                wire = type(v).__name__
                made = self.spec.leaves[wire](state, v)
                return tree.Leaf(wire, v), made
            case [Node.STAYS, str(hid)]:
                obj = self.given[hid]
                return tree.Stays(type(obj).__name__, obj), obj
            case [Node.ITEMS, list(items)]:
                made = self.spec.items(state)
                nodes = []
                for i in items:
                    n, m = self.node(state, i)
                    self.spec.add_item(state, made, m)
                    nodes.append(n)
                return tree.Items(type(made).__name__, made, nodes), made
            case [Node.ENTRIES, dict(entries)]:
                made = self.spec.entries(state)
                named = {}
                for k, v in entries.items():
                    n, m = self.node(state, v)
                    self.spec.add_entry(state, made, k, m)
                    named[k] = n
                return tree.Entries(type(made).__name__, made, named), made
        raise ProtocolError(f"a data node arrived as {raw!r:.80}")


def _roles(specs: Iterable[Call]) -> dict[Subscription, Call]:
    """The calls among `specs` that open and close a shared
    subscription, by the role the declaration gives them."""
    return {s.subscription: s for s in specs if s.subscription is not None}


def _method(spec: Call) -> Callable[..., Awaitable[Any]]:
    """The call on the object a handle names. By the method, not
    through a runner: a log reader is a fan-out `Reader`, duck-typed as
    the class the spec belongs to."""
    async def run(token: str, target: Any, *args: Any) -> Any:
        return await getattr(target, spec.name)(*args)
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
    def __init__(self, tasks: Any,
                 lease_ttl: float = 120.0,
                 escrow_ttl: float | None = 300.0,
                 callbacks: bool = False) -> None:
        """Every table it reads is emitted, in
        `huggorm_generated._policy`, so this reads them by name.

        The handlers are built in a loop and that is right: the
        body of one is a RULE - decode, call, encode - and it reads
        the same for every method. What differs is the spec, and the
        build writes that."""
        # `tasks` finishes what it holds: a runner shutdown releases
        # an affine thread from the collector's list, and cancelling
        # that is how a dead thread stays registered. It OWNS its
        # children, so nothing here retains a set of tasks by hand
        # (huggorm#35).
        self.tasks = tasks
        self.table = HandleTable(ttl=lease_ttl, escrow_ttl=escrow_ttl)
        # EXPERIMENTAL (huggorm#153): whether a client may hand over an
        # object the server calls back.
        self.callbacks = callbacks
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
        self._log_fanouts: dict[int, Fanout] = {}
        # The `@subscription` calls, routed through the fan-outs.
        # Called straight through, an OPEN REPLACES the thread's
        # subscription, and an open `Session/Logs` stream on that
        # state goes connected and silent (huggorm#85). So the handle a
        # remote OPEN answers is a fan-out reader, and a remote CLOSE
        # leaves only the readers that connection opened. Keyed by the
        # declared role, so no layer above the bindings names a method
        # or a type (`test_no_hardcoded_domain_types`).
        self._subscriptions = {cls: _roles(specs)
                               for cls, specs in METHODS.items()}
        self._method_roles: dict[Subscription, Callable[..., Awaitable[Any]]] = {
            Subscription.OPEN: self._subscribe_logs,
            Subscription.CLOSE: self._unsubscribe_logs,
        }
        self._free_roles: dict[Subscription, Callable[..., Awaitable[Any]]] = {
            Subscription.OPEN: self._subscribe_process_logs,
            Subscription.CLOSE: self._unsubscribe_process_logs,
        }
        # One fan-out for the process sink. Not a map, because there
        # is one sink and nothing to key by, and not created lazily
        # for the same reason.
        process = _roles(FREE.values())
        self._process_fanout = Fanout(
            lambda capacity, level: FUNCTIONS[
                process[Subscription.OPEN].name](capacity, level),
            lambda: FUNCTIONS[process[Subscription.CLOSE].name]())
        # Weak, because the handle table and the fan-out are what keep
        # a reader alive.
        self._handle_readers: dict[tuple[str, int], weakref.WeakSet[Reader]] = {}
        # A failure crosses the same way a value does: by what the
        # bindings declare, never by a type this file names
        # (huggorm#36).
        self.faults = Faults(self.codec)
        self.handlers = self._handlers()

    def _fanout(self, target: Any) -> Fanout:
        """This state's fan-out, made on first use.

        Its OPEN and CLOSE run on the state's own runner, so the
        fan-out never has to name the target."""
        key = id(target)
        fan = self._log_fanouts.get(key)
        if fan is None:
            roles = self._subscriptions[DECLARED[type(target)]]
            runner = _runner(target)
            fan = Fanout(
                lambda capacity, level: runner.call(
                    roles[Subscription.OPEN], [capacity, level]),
                lambda: runner.call(roles[Subscription.CLOSE], []))
            self._log_fanouts[key] = fan
        return fan

    async def _join(self, fan: Fanout, key: tuple[str, int],
                    capacity: int, level: int) -> Reader:
        reader = await fan.join(capacity, level)
        self._handle_readers.setdefault(key, weakref.WeakSet()).add(reader)
        return reader

    async def _leave_all(self, key: tuple[str, int]) -> None:
        for reader in list(self._handle_readers.pop(key, ())):
            await reader.close()

    async def _subscribe_logs(self, token: str, target: Any,
                              capacity: int, level: int) -> Reader:
        return await self._join(self._fanout(target), (token, id(target)),
                                capacity, level)

    async def _unsubscribe_logs(self, token: str, target: Any) -> None:
        await self._leave_all((token, id(target)))

    async def _subscribe_process_logs(self, token: str,
                                      capacity: int, level: int) -> Reader:
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
            # Shielded: a server that stops cancels this group, and a
            # state must still leave its own thread.
            # Nothing to report a failure to: the connection that owned
            # this handle is already gone.
            with anyio.CancelScope(shield=True), \
                    contextlib.suppress(Exception):
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
        name = type(obj).__name__
        cls = CLASSES.get(name)
        if cls is None:
            raise TypeError(
                f"{name} has no async wrapper, so it cannot be handed out "
                f"as a handle")
        return cls._adopt(obj, _runner(parent))

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
        handlers: dict[int, CallHandler] = {}
        for cls_name, methods in METHODS.items():
            for m in methods:
                handlers[m.index] = CallHandler(
                    Target.HANDLE,
                    (self._method_roles[m.subscription] if m.subscription
                     else _method(m)),
                    f"{cls_name}.{m.name}")
        for cls_name, acquire in ACQUIRE.items():
            handlers[acquire.index] = CallHandler(
                Target.NEW, _construct(CLASSES[cls_name]),
                f"{cls_name}.Acquire")
        for fname, call in FREE.items():
            handlers[call.index] = CallHandler(
                Target.NONE,
                (self._free_roles[call.subscription] if call.subscription
                 else _function(FUNCTIONS[fname])),
                f"Functions.{fname}")
        listed = {c.index for c in CALLS}
        if handlers.keys() != listed:
            raise RuntimeError(
                f"calls {sorted(listed - handlers.keys())} have no handler, "
                f"and handlers {sorted(handlers.keys() - listed)} answer no "
                f"listed call")
        return handlers

    def _arguments(self, spec: Call | Acquire, raw: list[Any],
                   resolve: Callable[[str], Any],
                   back: Callbacks | None = None) -> list[Any]:
        """A call's decoded arguments. A constructor may be sent fewer
        than it declares, and the binding fills in the defaults."""
        least = spec.required if isinstance(spec, Acquire) else len(spec.args)
        if not least <= len(raw) <= len(spec.args):
            raise TypeError(
                f"{len(raw)} argument(s) for {least}..{len(spec.args)}")
        client_obj = None if back is None else back.obj
        return [self.codec.decode(a.type, v, resolve, client_obj=client_obj)
                for a, v in zip(spec.args, raw, strict=False)]

    async def _run_call(self, token: str, index: int, hid: Any,
                        raw: list[Any], back: Callbacks | None = None) -> Any:
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
                                     *self._arguments(spec, raw, resolve, back))
                return self.codec.encode(
                    returns, result,
                    lambda obj: self.put(obj, token, parents=[hid]))
            case Target.NEW:
                return self.put(
                    await h.run(token, *self._arguments(spec, raw, resolve, back)),
                    token)
            case Target.NONE:
                result = await h.run(token,
                                     *self._arguments(spec, raw, resolve, back))
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
                      budget: int, force: bool = False,
                      ) -> tuple[Any, tree.Node, TreeWalk]:
        """One round trip for a whole value tree: the target, the tree,
        and the walk that built it.

        Walking a value from the client is a call per node, and
        every one of them is a network round trip plus a thread
        handover. This walks it once, on the value's own thread.

        It forces nothing unless `force` asks. What is already forced
        serializes; a thunk crosses as a handle, and the caller forces
        it with the call that already exists. A forcing walk forces
        what it visits, inside the same bounds, and a node whose force
        throws crosses as a handle - so it still cannot raise halfway
        down a half-built message (huggorm#147)."""
        target = self.resolve(hid, token)
        spec = TREES.get(DECLARED.get(type(target), ""))
        if spec is None:
            raise TypeError(
                f"{hid[:8]} is not a value tree: its type declares no walk")
        walk = TreeWalk(spec, depth if depth > 0 else DEFAULT_DEPTH,
                        budget if budget > 0 else DEFAULT_BUDGET, force)
        # ONE hop for the whole tree, on the value's own thread.
        root = await _runner(target).run(lambda obj: walk.node(obj, 0))
        return target, root, walk

    async def build(self, token: str, hid: str, data: Any) -> list[Any]:
        """A value made from Python data, in one hop on the state's own
        thread, answered as the tree it built (huggorm#147).

        A handle inside the data stands for the value it names, and
        answers under the same id, so the caller keeps its own object
        there. A value of another state is refused: its attribute names
        index another symbol table. Every value made leases to the
        caller and pins the state, as a method's answer does."""
        from huggorm_generated._runtime import _check_isolation, unwrap_arg

        target = self.resolve(hid, token)
        spec = BUILDERS.get(DECLARED.get(type(target), ""))
        if spec is None:
            raise TypeError(
                f"{hid[:8]} makes no values: its type declares no builders")
        given = {h: self.resolve(h, token) for h in Build.handles(data)}
        _check_isolation(_runner(target), given.values())
        build = Build(spec, {h: unwrap_arg(w) for h, w in given.items()})
        root = await _runner(target).run(lambda obj: build.root(obj, data))
        known = {id(obj): h for h, obj in build.given.items()}
        return self.codec.encode_tree(
            root, lambda _cls, obj: known.get(id(obj)) or self.put(
                self.adopt(obj, target), token, parents=[hid]))

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
        request: int = await _runner(target).run(lambda _: current_request())
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
            case Control.REALIZE, [str(hid), int(depth), int(budget),
                                   bool(force)]:
                target, root, _walk = await self.realize(token, hid, depth,
                                                         budget, force)
                # Every node the walk stopped at leases to the caller
                # and pins the root, exactly as a proxy return does.
                return self.codec.encode_tree(
                    root, lambda _cls, obj: self.put(
                        self.adopt(obj, target), token, parents=[hid]))
            case Control.BUILD, [str(hid), list(data)]:
                return await self.build(token, hid, data)
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
        that detached claims them back. A token that holds no lease
        and no other stream is forgotten."""
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
        bound = self.table.connections[token]
        try:
            await channel.send([Op.WELCOME, token, ttl])
            await self._serve(channel, token)
        finally:
            self.table.unbind(token, bound)

    async def _serve(self, channel: Channel, token: str) -> None:
        running: dict[int, anyio.CancelScope] = {}
        back = (Callbacks(channel, self.codec, self.faults)
                if self.callbacks else None)
        async with anyio.create_task_group() as calls:
            try:
                await self._frames(channel, token, calls, running, back)
            finally:
                # Before the calls are cancelled: a thread waiting on a
                # callback must fail, or it never lets its call end.
                if back is not None:
                    back.close()
                calls.cancel_scope.cancel()

    async def _frames(self, channel: Channel, token: str, calls: Any,
                      running: dict[int, anyio.CancelScope],
                      back: Callbacks | None = None) -> None:
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
                                          args, back))
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
                case [Op.REPLY | Op.REFUSE, int(), _] if back is not None:
                    back.answer(frame)
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
                callbacks: bool = False,
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
    sweeper, which never returns on its own, so waiting for it would
    hang the shutdown. Being inner is what orders the two: the loops
    stop first, and anything they started is still awaited by `work`
    afterwards.

    That split is the trap huggorm#35 names first: a task group does
    not cancel its children on exit, it waits for them.

    The socket is mode 0600, and a connection checks the peer's uid
    as well: only this uid may connect."""
    # One `with`, two groups, and the ORDER inside it is the whole
    # point: `loops` is entered second, so it exits first. Ruff asks
    # for the combined form and it says the same thing.
    async with (anyio.create_task_group() as work,
                anyio.create_task_group() as loops):
        dispatcher = Dispatcher(work, lease_ttl=lease_ttl,
                                escrow_ttl=escrow_ttl, callbacks=callbacks)

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
            # Every state closes on its own thread, which `work` awaits.
            dropped = dispatcher.table.close()
            logger.info("stopping: closing %d handle(s)", len(dropped))


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
    parser.add_argument(
        "--experimental-callbacks", action="store_true",
        help="let a client hand over objects this server calls back, "
             "such as a SourceAccessor (huggorm#153). EXPERIMENTAL and "
             "unsafe for a state that several clients share")
    args = parser.parse_args(argv)
    for name, value in args.option:
        set_setting(name, value)
    anyio.run(_until_signalled, functools.partial(
        serve, args.path, args.ttl, callbacks=args.experimental_callbacks))


async def _until_signalled(
        serving: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Serve until SIGTERM or SIGINT, then stop as a cancel does: the
    calls in flight are interrupted, and every state is closed on its
    own thread.

    Nix checks for an interrupt between system calls, not inside one, so
    a call that blocks can hold the stop open. After the first signal
    both take their default effect, so a second one stops the process
    at once. Closing the receiver is not enough: it puts back asyncio's
    SIGINT handler, which only cancels again."""
    async with anyio.create_task_group() as tg:
        tg.start_soon(serving)
        with anyio.open_signal_receiver(signal.SIGTERM,
                                        signal.SIGINT) as signals:
            async for signum in signals:
                logger.info("stopping on %s", signal.Signals(signum).name)
                break
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, signal.SIG_DFL)
        tg.cancel_scope.cancel()


if __name__ == "__main__":
    main()
