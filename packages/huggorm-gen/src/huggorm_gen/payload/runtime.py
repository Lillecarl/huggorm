"""
Hand-written runtime for generated wrappers. NOT generated.

Two execution strategies:
- AffineRunner: one dedicated thread per wrapper. The target object is
  CONSTRUCTED on that thread and every call hops to it. Use for classes
  whose C++ implementation is not thread-safe.
- PoolRunner: a shared thread pool. Any thread may touch the object.
  Use for thread-safe classes.

Both bridge the sync C++ calls into asyncio via run_in_executor.

Exception policy:
- Typed errors (WrapperError subclasses) pass through untouched.
  They are recognized by duck-typing: they carry a to_dict() method,
  which keeps error handling uniform regardless of where the failure came from.
- Anything else is wrapped in InternalError with the original as
  __cause__, so C++ exceptions and programming bugs arrive uniformly.
- Construction failures are cached and re-raised on every call; we do
  not retry a factory that already failed.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import copy
import threading
from collections.abc import Callable, Iterable
from typing import Any

_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()
# How many blocking calls may be in flight at once. Four is a default,
# not a law: a store call talks to a daemon or a database and spends
# most of its time waiting, so an application doing bulk work wants
# more, and one embedded beside other thread pools may want fewer.
_POOL_SIZE = 4


def set_pool_size(workers: int) -> None:
    """Size the shared pool, before anything uses it.

    Every pool-threaded call in every generated wrapper runs here, so
    four concurrent blocking store calls saturate the default and the
    fifth waits. A library cannot guess the right number - it depends
    on the application, not on this package - so it takes one.

    Raises once the pool exists. Resizing a live ThreadPoolExecutor is
    not something concurrent.futures offers, and pretending otherwise
    would silently keep the old size."""
    global _POOL_SIZE
    if workers < 1:
        raise ValueError(f"pool size must be at least 1, got {workers}")
    with _POOL_LOCK:
        if _POOL is not None:
            raise RuntimeError(
                "the shared pool is already running; set_pool_size must be "
                "called before the first pool-threaded call")
        _POOL_SIZE = workers


class WrapperError(Exception):
    """Base for typed, serializable errors raised inside wrapped targets."""

    code = "wrapper_error"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    # No from_dict. Rebuilding an error is a WIRE concern and it lives
    # at the wire, where the manifest says which classes exist. The
    # version that lived here approximated the cause from a hard-coded
    # map of five builtins - a list of library knowledge in the one
    # module that is emitted beside the wrappers and must not know
    # which library it wraps (tasks/036).


class InternalError(WrapperError):
    """Wrapper for unexpected errors (C++ exceptions, bugs)."""

    code = "internal"

    def __init__(self, message: str, cause: BaseException):
        super().__init__(message)
        self.__cause__ = cause

    def to_dict(self) -> dict[str, str]:
        d = super().to_dict()
        cause = self.__cause__
        d["cause_type"] = type(cause).__name__
        d["cause_message"] = str(cause)
        return d


def _shared_pool() -> concurrent.futures.ThreadPoolExecutor:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=_POOL_SIZE,
                thread_name_prefix="huggorm-pool",
            )
        return _POOL


_REQUEST_LOCK = threading.Lock()
_REQUEST_SEQ = 0


def _next_request() -> int:
    """One number per wrapped call, for the life of the process.

    A LOCK rather than `itertools.count`, whose atomicity is a
    CPython implementation detail and not a promise. The cost is paid
    once per call, beside a thread handover that costs far more.

    Never 0. Zero is what a record carries when no wrapped call was on
    the thread that raised it - a fetcher, a file transfer, a build -
    so a real call must never collide with it."""
    global _REQUEST_SEQ
    with _REQUEST_LOCK:
        _REQUEST_SEQ += 1
        return _REQUEST_SEQ


class _InRequest:
    """Name the call, ON the thread that runs it.

    Every wrapped call enters C++ through one of three places, and all
    three use this: a record raised anywhere else carries 0, and that
    has to mean "nix's own thread" rather than "a hole in the
    chokepoint".

    The id is allocated on the LOOP thread and passed in, because
    `run_in_executor` carries no context - so an explicit argument is
    the only plumbing that works.

    The bindings are imported here rather than at module scope,
    exactly as `_release_gc_thread` does it: the runtime otherwise
    knows nothing about them."""

    __slots__ = ("_previous", "_request")

    def __init__(self, request: int) -> None:
        self._request = request
        self._previous = 0

    def __enter__(self) -> None:
        import huggorm_bindings

        self._previous = huggorm_bindings.begin_request(self._request)

    def __exit__(self, *exc: object) -> None:
        import huggorm_bindings

        # Pushes the "finalized" marker for the id that is ending, and
        # puts back whatever this thread was inside before.
        huggorm_bindings.end_request(self._previous)


def _refuse_foreign(callee: Any, arg: Any, x: Any) -> None:
    """Refuse a proxy that belongs to another isolation.

    An affine object IS its own isolation. A `nix::Value` is only
    meaningful to the `EvalState` that allocated it - an attribute
    name is a `Symbol`, an index into that state's own table - so
    handing one to a second state reads whatever that state's table
    holds at the same index. Not an error there: a wrong answer, or a
    crash.

    The EXECUTOR is the identity, not the runner. A value produced by
    a state gets an `AttachedRunner` over the producer's executor, so
    two objects belong to the same isolation exactly when they run on
    the same thread - which is also why one state per thread is the
    rule this can be checked against.

    Only between two DEDICATED-thread runners. A pool object is
    thread-safe by declaration and belongs to no isolation, so passing
    a Store to a state's method is not this.

    Enforced HERE and not in the binding, and Carl decided that: the
    async layer is the lowest one that manages threads for a caller,
    so it is where a library user meets the rule. A caller using the
    sync binding directly is on their own - the C++ is exactly as
    permissive as libexpr, which has no such rule of its own.
    """
    if not (callee.dedicated_thread and arg.dedicated_thread):
        return
    if callee._executor() is arg._executor():
        return
    raise TypeError(
        f"{type(x).__name__} belongs to another EvalState. A value is "
        f"only meaningful to the state that allocated it, because an "
        f"attribute name is an index into that state's own symbol "
        f"table - so this would read the wrong table rather than fail. "
        f"Force it to data and copy it across, or do the work on the "
        f"state that owns it.")


def unwrap_arg(x: Any) -> Any:
    """Normalize one argument: an async wrapper contributes its target
    object, anything else passes through. Used for method arguments and
    for constructor args replayed by a lazy factory.

    Wire-value types cross boundaries as COPIES - the local emulation
    of serialization, so a future remote transport changes nothing
    about aliasing semantics. Requires the sync binding to expose
    __copy__; immutable types should always do so."""
    r = getattr(x, "_runner", None)
    if r is None:
        return x
    obj = r.ensure()
    if getattr(x, "_wire", "proxy") == "value":
        return copy.copy(obj)
    return obj


def _check_isolation(callee: Any, args: Iterable[Any]) -> None:
    """Every argument of one call, against the isolation receiving it.

    BEFORE the executor hop and before `_materialize_args`, which is
    why it is here rather than inside `unwrap_arg`. Two reasons, and
    the first is the one that made it move: `_invoke` wraps anything
    it catches in an `InternalError`, so a refusal raised down there
    reached a caller as "force failed" with the reason buried in a
    cause. A rule the caller has to act on may not arrive as an
    internal bug. The second is cheaper: refusing costs no thread
    handover, and materializes nothing on behalf of a call that will
    not happen.

    `run` is NOT checked, and needs no check: it takes a FUNCTION
    rather than wrapper arguments, so there is nothing to compare.
    Its one caller walks the target's own value on the target's own
    thread. `call_function` is not checked either - a free function
    belongs to no isolation, so there is nothing for an argument to
    be foreign TO."""
    for x in args:
        r = getattr(x, "_runner", None)
        if r is None:
            continue
        if getattr(x, "_wire", "proxy") == "value":
            # A wire value crosses as a COPY, so it carries no tie to
            # whatever made it. That is the one crossing the rule
            # allows - forced into data and copied.
            #
            # UNREACHABLE TODAY, and measured: removing this branch
            # fails nothing, because every wire value in the corpus is
            # produced by a POOL class and the guard below already
            # skips those. It stays because it is the rule rather than
            # an optimisation - the day an affine class returns a wire
            # value, refusing the copy would be wrong. Said here so it
            # is not read as covered (`tasks/075`'s lesson).
            continue
        _refuse_foreign(callee, r, x)


async def _materialize_args(args: list[Any]) -> None:
    """Give every wrapper argument something to unwrap.

    Each one constructs on ITS OWN thread, one at a time, before the
    call hops to the callee's thread. No runner's thread is held while
    this runs, so an affine callee waiting on an affine argument cannot
    deadlock."""
    for a in args:
        r = getattr(a, "_runner", None)
        if r is not None and r._obj is None:
            await r.materialize()


class BaseRunner:
    # Dedicated-thread runners (affine/attached) may only construct on
    # their own thread; pool runners may construct anywhere.
    dedicated_thread = False

    def __init__(self, factory: Callable[[], Any] | None,
                 obj: Any = None) -> None:
        # Either a lazy factory (constructed on first use, on this
        # runner's thread) or an already-built obj — never both.
        self._factory = factory
        self._obj: Any = obj
        self._construct_error: BaseException | None = None
        # Pool runners admit concurrent first-calls; this lock makes
        # check-and-construct atomic so the factory runs exactly once.
        self._construct_lock = threading.Lock()
        self.last_worker_name: str | None = None
        self.last_worker_ident: int | None = None
        self.born_thread_name: str | None = None
        self.workers_seen: set[str] = set()

    def _resolve(self) -> Any:
        # Runs inside a worker thread. First call constructs the object
        # on whichever thread this runner owns (affine) or a pool thread.
        # Double-checked: the fast path skips the lock once constructed.
        if self._obj is not None:
            return self._obj
        with self._construct_lock:
            if self._obj is None:
                if self._construct_error is not None:
                    # Factory already failed once; re-raise without retrying.
                    raise self._construct_error
                try:
                    assert self._factory is not None
                    self._obj = self._factory()
                except Exception as e:
                    self._construct_error = e
                    raise
                self.born_thread_name = threading.current_thread().name
        return self._obj

    def ensure(self) -> Any:
        """Return the underlying object, constructing it if needed.
        Used when a wrapper is passed as an argument to another wrapper:
        the sync binding wants the raw target, not the async handle.

        Construction normally happens on the runner's own thread via
        _invoke -> _resolve. This method therefore runs OFF-home by
        definition, so a dedicated-thread runner refuses to construct
        here: silently building an affine object on a foreign thread is
        exactly the corruption this layer exists to prevent."""
        if self.dedicated_thread and self._obj is None:
            if self._construct_error is not None:
                raise self._construct_error
            raise TypeError(
                "affine wrapper used as an argument before its first "
                "call: it can only be constructed on its own thread. "
                "Call a method on it first, or declare the type 'pool'.")
        return self._resolve()

    @staticmethod
    def _unwrap(args: Iterable[Any]) -> list[Any]:
        # A wrapper argument contributes its target object.
        return [unwrap_arg(a) for a in args]

    def _invoke(self, method: str, args: list[Any], request: int) -> Any:
        try:
            with _InRequest(request):
                obj = self._resolve()
                attr = getattr(obj, method)
                args = self._unwrap(args)
                # Properties resolve to values, not callables: reading
                # one still runs on this runner's thread, which is the
                # point.
                return attr(*args) if callable(attr) else attr
        except Exception as e:
            if hasattr(e, "to_dict"):
                raise  # typed wrapper error — pass through untouched
            raise InternalError(f"{method} failed", cause=e) from e
        finally:
            cur = threading.current_thread()
            self.last_worker_name = cur.name
            self.last_worker_ident = cur.ident
            self.workers_seen.add(cur.name)

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        raise NotImplementedError

    async def aclose(self) -> None:
        """Release whatever this runner owns. Every subclass has always
        had one; the base did not declare it, so every emitted wrapper
        called an attribute that formally did not exist."""
        raise NotImplementedError

    async def materialize(self) -> None:
        """Construct the target on this runner's OWN thread.

        ensure() refuses to build a dedicated-thread object, because it
        runs off-home by definition. That is right, and it left a hole:
        an affine wrapper could not be passed as an argument until
        something else had happened to construct it. So describe(store)
        worked or failed depending on whether the caller had touched
        the store first - in process and over the wire alike.

        The fix is not to relax the refusal but to satisfy it. This
        hops to the runner's own executor and constructs there, exactly
        as a real call would, so by the time unwrap_arg sees the
        wrapper there is an object to take."""
        if self._obj is not None:
            return
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(self._executor(), self._resolve)

    async def run(self, fn: Callable[[Any], Any]) -> Any:
        """Run one function against the target, on the target's own
        executor.

        `call` dispatches a method by name, which costs a thread
        handover per call. This is for work that has to visit an object
        many times - walking a value that holds values - and must not
        pay a handover for every visit. The function runs on the home
        thread, so everything it touches is on that thread too."""
        await self.materialize()
        loop = asyncio.get_running_loop()
        request = _next_request()

        def invoke() -> Any:
            try:
                with _InRequest(request):
                    return fn(self.ensure())
            except Exception as e:
                if hasattr(e, "to_dict"):
                    raise
                raise InternalError("run failed", cause=e) from e

        return await loop.run_in_executor(self._executor(), invoke)

    async def call(self, method: str, args: list[Any]) -> Any:
        _check_isolation(self, args)
        await _materialize_args(args)
        loop = asyncio.get_running_loop()
        # Allocated HERE, on the loop thread, and passed in.
        # run_in_executor carries no context, so a contextvar set on
        # this side would not reach the thread that does the work.
        request = _next_request()
        return await loop.run_in_executor(
            self._executor(), lambda: self._invoke(method, args, request))


def _release_gc_thread() -> None:
    """Run ON a dying dedicated thread, as its last action.

    The bindings are imported here rather than at module scope: the
    runtime otherwise knows nothing about them, and this is the one
    thing it cannot do without them."""
    import huggorm_bindings

    huggorm_bindings.gc_release_thread()


class AffineRunner(BaseRunner):
    """All operations (including construction) run on one dedicated thread."""

    dedicated_thread = True

    def __init__(self, factory: Callable[[], Any],
                 name: str = "huggorm-affine") -> None:
        super().__init__(factory)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=name
        )

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return self._pool

    async def aclose(self) -> None:
        # The dedicated thread is about to die, so it has to leave the
        # collector's list first - on itself, as its last GC action.
        # Skipping that left a dead thread registered, and the next
        # collection aborted the whole process (the server's reaper
        # closes affine wrappers, so this reached production paths).
        # Submitted BEFORE shutdown, so it is the last work this
        # single-worker executor accepts and runs.
        self._pool.submit(_release_gc_thread)
        # Blocking shutdown is acceptable here: the queue is empty once
        # pending awaits finish. Move to a thread if this ever matters.
        self._pool.shutdown(wait=True)


class PoolRunner(BaseRunner):
    """Operations run on a shared pool. Target must be thread-safe."""

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return _shared_pool()

    async def aclose(self) -> None:
        return None  # shared pool outlives wrappers


class AttachedRunner(BaseRunner):
    """
    Wraps an already-constructed object and runs every operation on
    ANOTHER runner's executor. Used for affine returned types: the child
    inherits the producer's dedicated thread, so its ops serialize with
    the producer's own.
    """

    dedicated_thread = True

    def __init__(self, obj: Any, parent: BaseRunner) -> None:
        super().__init__(factory=None, obj=obj)
        self._parent = parent
        # Best knowledge: the producer's last worker is where obj was born.
        self.born_thread_name = parent.last_worker_name

    def _executor(self) -> concurrent.futures.ThreadPoolExecutor:
        return self._parent._executor()

    async def aclose(self) -> None:
        # Nothing to shut down: the executor is shared with the parent,
        # and the underlying C++ object dies with this wrapper.
        self._obj = None


async def call_function(fn: Callable[..., Any], args: list[Any]) -> Any:
    """Run a module-level binding function on the shared pool.

    Free functions have no instance, so there is no runner to own them
    and no lazy construction to do - only the executor hop and the same
    error policy every wrapped call gets. Arguments still go through
    unwrap_arg: a caller holding an async wrapper passes it here exactly
    as it would to a method."""
    await _materialize_args(args)
    loop = asyncio.get_running_loop()
    request = _next_request()

    def invoke() -> Any:
        try:
            with _InRequest(request):
                return fn(*[unwrap_arg(a) for a in args])
        except Exception as e:
            if hasattr(e, "to_dict"):
                raise  # typed wrapper error - pass through untouched
            raise InternalError(f"{fn.__name__} failed", cause=e) from e

    return await loop.run_in_executor(_shared_pool(), invoke)


def attach_runner(obj: Any, parent: BaseRunner, policy: str) -> BaseRunner:
    """Pick a runner for a returned object based on its declared policy."""
    if policy == "affine":
        if isinstance(parent, AffineRunner | AttachedRunner):
            return AttachedRunner(obj, parent)
        raise TypeError(
            "affine return type produced on a pool runner: the object has "
            "no home thread. Declare it 'pool' or produce it from an "
            "affine wrapper."
        )
    if policy == "pool":
        return PoolRunner(None, obj=obj)
    raise ValueError(f"unknown threading policy {policy!r}")
