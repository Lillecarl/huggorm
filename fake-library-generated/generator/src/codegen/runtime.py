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

import asyncio
import concurrent.futures
import copy
import threading

_POOL = None
_POOL_LOCK = threading.Lock()


class WrapperError(Exception):
    """Base for typed, serializable errors raised inside wrapped targets."""

    code = "wrapper_error"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class InternalError(WrapperError):
    """Wrapper for unexpected errors (C++ exceptions, bugs)."""

    code = "internal"

    def __init__(self, message: str, cause: BaseException):
        super().__init__(message)
        self.__cause__ = cause

    def to_dict(self) -> dict:
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


def unwrap_arg(x):
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


class BaseRunner:
    def __init__(self, factory, obj=None):
        # Either a lazy factory (constructed on first use, on this
        # runner's thread) or an already-built obj — never both.
        self._factory = factory
        self._obj = obj
        self._construct_error = None
        self.last_worker_name = None
        self.last_worker_ident = None
        self.born_thread_name = None
        self.workers_seen = set()

    def _resolve(self):
        # Runs inside a worker thread. First call constructs the object
        # on whichever thread this runner owns (affine) or a pool thread.
        if self._obj is None:
            if self._construct_error is not None:
                # Factory already failed once; re-raise without retrying.
                raise self._construct_error
            try:
                self._obj = self._factory()
            except Exception as e:
                self._construct_error = e
                raise
            self.born_thread_name = threading.current_thread().name
        return self._obj

    def ensure(self):
        """Return the underlying object, constructing it if needed.
        Used when a wrapper is passed as an argument to another wrapper:
        the sync binding wants the raw target, not the async handle."""
        return self._resolve()

    @staticmethod
    def _unwrap(args):
        # A wrapper argument contributes its target object.
        return [unwrap_arg(a) for a in args]

    def _invoke(self, method, args):
        cur_before = threading.current_thread()
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

    async def call(self, method: str, args: list):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor(), lambda: self._invoke(method, args))


class AffineRunner(BaseRunner):
    """All operations (including construction) run on one dedicated thread."""

    def __init__(self, factory, name: str = "flg-affine"):
        super().__init__(factory)
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=name
        )

    def _executor(self):
        return self._pool

    async def aclose(self):
        # Blocking shutdown is acceptable here: the queue is empty once
        # pending awaits finish. Move to a thread if this ever matters.
        self._pool.shutdown(wait=True)


class PoolRunner(BaseRunner):
    """Operations run on a shared pool. Target must be thread-safe."""

    def _executor(self):
        return _shared_pool()

    async def aclose(self):
        return None  # shared pool outlives wrappers


class AttachedRunner(BaseRunner):
    """
    Wraps an already-constructed object and runs every operation on
    ANOTHER runner's executor. Used for affine returned types: the child
    inherits the producer's dedicated thread, so its ops serialize with
    the producer's own.
    """

    def __init__(self, obj, parent: BaseRunner):
        super().__init__(factory=None, obj=obj)
        self._parent = parent
        # Best knowledge: the producer's last worker is where obj was born.
        self.born_thread_name = parent.last_worker_name

    def _executor(self):
        return self._parent._executor()

    async def aclose(self):
        # Nothing to shut down: the executor is shared with the parent,
        # and the underlying C++ object dies with this wrapper.
        self._obj = None


def attach_runner(obj, parent: BaseRunner, policy: str) -> BaseRunner:
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
