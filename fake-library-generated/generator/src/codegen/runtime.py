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
from typing import Any, Callable, Iterable

_POOL: concurrent.futures.ThreadPoolExecutor | None = None
_POOL_LOCK = threading.Lock()


class WrapperError(Exception):
    """Base for typed, serializable errors raised inside wrapped targets."""

    code = "wrapper_error"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}

    @classmethod
    def from_dict(cls, d: dict[str, str]) -> WrapperError:
        """Rebuild a wrapper error from to_dict() output - used at RPC
        boundaries so typed errors survive the wire. A dict carrying a
        cause is by definition an InternalError; the original cause
        type is approximated by name."""
        known: dict[str, type[BaseException]] = {
                 "ValueError": ValueError, "TypeError": TypeError,
                 "RuntimeError": RuntimeError, "KeyError": KeyError,
                 "OSError": OSError}
        if "cause_type" not in d:
            return WrapperError(d.get("message", ""))
        cause_cls = known.get(d["cause_type"], Exception)
        cause = cause_cls(d.get("cause_message", ""))
        return InternalError(d.get("message", ""), cause=cause)


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
                max_workers=4, thread_name_prefix="flg-pool"
            )
        return _POOL


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

    def _invoke(self, method: str, args: list[Any]) -> Any:
        try:
            obj = self._resolve()
            attr = getattr(obj, method)
            args = self._unwrap(args)
            # Properties resolve to values, not callables: reading one
            # still runs on this runner's thread, which is the point.
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

    async def call(self, method: str, args: list[Any]) -> Any:
        await _materialize_args(args)
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor(), lambda: self._invoke(method, args))


def _release_gc_thread() -> None:
    """Run ON a dying dedicated thread, as its last action.

    The bindings are imported here rather than at module scope: the
    runtime otherwise knows nothing about them, and this is the one
    thing it cannot do without them."""
    import fake_library

    fake_library.gc_release_thread()


class AffineRunner(BaseRunner):
    """All operations (including construction) run on one dedicated thread."""

    dedicated_thread = True

    def __init__(self, factory: Callable[[], Any],
                 name: str = "flg-affine") -> None:
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

    def invoke() -> Any:
        try:
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
