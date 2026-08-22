"""
Hand-written runtime for generated clients. NOT generated.

Two execution strategies:
- AffineRunner: one dedicated thread per client. The service object is
  CONSTRUCTED on that thread and every call hops to it. Use for classes
  whose C++ implementation is not thread-safe.
- PoolRunner: a shared thread pool. Any thread may touch the object.
  Use for thread-safe classes.

Both bridge the sync C++ calls into asyncio via run_in_executor.
"""

import asyncio
import concurrent.futures
import threading

_POOL = None
_POOL_LOCK = threading.Lock()


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
        self.last_worker_name = None
        self.last_worker_ident = None
        self.born_thread_name = None
        self.workers_seen = set()

    def _resolve(self):
        # Runs inside a worker thread. First call constructs the object
        # on whichever thread this runner owns (affine) or a pool thread.
        if self._obj is None:
            self._obj = self._factory()
            self.born_thread_name = threading.current_thread().name
        return self._obj

    def _invoke(self, method, args):
        obj = self._resolve()
        result = getattr(obj, method)(*args)
        cur = threading.current_thread()
        self.last_worker_name = cur.name
        self.last_worker_ident = cur.ident
        self.workers_seen.add(cur.name)
        return result

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
