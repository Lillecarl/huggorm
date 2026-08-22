"""
Hand-written runtime for generated clients. NOT generated.

Two execution strategies:
- AffineRunner: one dedicated thread per client. The service object is
  CONSTRUCTED on that thread and every call hops to it. Use for classes
  whose C++ implementation is not thread-safe.
- PoolRunner: a shared thread pool. Any thread may touch the object.
  Use for thread-safe classes.

Both bridge the sync C++ calls into asyncio via run_in_executor.

Exception policy:
- Errors from the IDL (ServiceError subclasses) pass through untouched.
  They are recognized by duck-typing: they carry a to_dict() method,
  which is also what will serialize them over RPC later.
- Anything else is wrapped in InternalError with the original as
  __cause__, so C++ exceptions and programming bugs arrive uniformly.
- Construction failures are cached and re-raised on every call; we do
  not retry a factory that already failed.
"""

import asyncio
import concurrent.futures
import threading

_POOL = None
_POOL_LOCK = threading.Lock()


class ServiceError(Exception):
    """Base for typed, serializable errors raised inside services."""

    code = "service_error"

    def __init__(self, message: str = ""):
        super().__init__(message)
        self.message = message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


class InternalError(ServiceError):
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


class BaseRunner:
    def __init__(self, factory):
        self._factory = factory
        self._obj = None
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

    def _invoke(self, method, args):
        cur_before = threading.current_thread()
        try:
            obj = self._resolve()
            return getattr(obj, method)(*args)
        except Exception as e:
            if hasattr(e, "to_dict"):
                raise  # typed service error — pass through untouched
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
        return None  # shared pool outlives clients
