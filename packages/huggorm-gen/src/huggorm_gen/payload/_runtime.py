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
import collections
import concurrent.futures
import contextlib
import copy
import functools
import importlib
import queue
import threading
import types
import weakref
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

import anyio
import anyio.from_thread
import anyio.lowlevel

_POOL: _Pool | None = None
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

    Raises once the pool exists. A live pool keeps the size it started
    with, and pretending otherwise would silently keep the old size."""
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
    # at the wire, where the emitted `_policy` says which classes
    # exist. This module is emitted beside the wrappers and must not
    # know which library it wraps, so it holds no map of library
    # errors to guess a cause from (huggorm#36).


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


def _shared_pool() -> _Pool:
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _Pool(_POOL_SIZE)
        return _POOL


# Nix's evaluator stack: `nix::setStackSize(60 * 1024 * 1024)` in
# `src/nix/main.cc`. Only the CLI's `main()` sets it, so an embedder's thread
# keeps the 8 MiB `RLIMIT_STACK` gives it, and the default `max-call-depth`
# of 10000 needs about 27 MB: `let f = n: f (n + 1); in f 0` segfaults the
# process instead of raising. A pthread stack is mmap'd and no rlimit bounds
# it. nanopynix measured all of this (`_core/_nix_executor.py`).
EVAL_STACK = 60 * 1024 * 1024
# `threading.stack_size` is process-global, so it is held only across the
# one spawn it is for.
_STACK_LOCK = threading.Lock()


def _start_with_eval_stack(pool: concurrent.futures.ThreadPoolExecutor) -> None:
    """Spawn `pool`'s single thread now, with `EVAL_STACK`."""
    with _STACK_LOCK:
        previous = threading.stack_size(EVAL_STACK)
        try:
            pool.submit(lambda: None).result()
        finally:
            threading.stack_size(previous)


class _Job:
    """One piece of work and the future it answers."""

    __slots__ = ("_fn", "future")

    def __init__(self, fn: Callable[[], Any]) -> None:
        self._fn = fn
        self.future: concurrent.futures.Future[Any] = concurrent.futures.Future()

    def run(self) -> None:
        if not self.future.set_running_or_notify_cancel():
            return
        try:
            result = self._fn()
        except BaseException as e:
            self.future.set_exception(e)
        else:
            self.future.set_result(result)


class _Pool(concurrent.futures.Executor):
    """The shared pool: `size` threads, plus one for each thread that
    waits for a hook (`wait`, huggorm#155).

    A waiting thread holds a Nix call open until the loop answers, and
    the answer may need pool work. So the pool does not count it, as
    Java's ForkJoinPool does for managed blocking. It does not run other
    work meanwhile, as `_Home` does: that work belongs to other calls,
    and would run deep inside this call's Nix stack.

    A thread over the count retires after its job. Its Boehm
    registration ends with it (`ThreadExit` in `gc.hpp`)."""

    def __init__(self, size: int, name: str = "huggorm-pool") -> None:
        self._size = size
        self._name = name
        self._queue: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        self._lock = threading.Lock()
        self._threads: set[threading.Thread] = set()
        self._started = 0
        self._idle = 0
        self._waiting = 0
        self._shutdown = False
        _POOLS.add(self)

    def submit(self, fn: Callable[..., Any], /, *args: Any,
               **kwargs: Any) -> concurrent.futures.Future[Any]:
        job = _Job(functools.partial(fn, *args, **kwargs))
        with self._lock:
            if self._shutdown:
                raise RuntimeError("cannot schedule new futures after shutdown")
            self._queue.put(job)
            self._grow()
        return job.future

    def _grow(self) -> None:
        """Under the lock: one more thread, when none is idle and fewer
        run than the size and the waiting threads allow."""
        if self._idle == 0 and len(self._threads) < self._size + self._waiting:
            thread = threading.Thread(
                target=self._work, name=f"{self._name}_{self._started}")
            self._started += 1
            self._threads.add(thread)
            thread.start()

    def _work(self) -> None:
        me = threading.current_thread()
        while True:
            with self._lock:
                if len(self._threads) > self._size + self._waiting:
                    self._threads.discard(me)
                    return
                self._idle += 1
            job = self._queue.get()
            with self._lock:
                self._idle -= 1
            if job is None:
                # Pass the stop on to the next thread.
                self._queue.put(None)
                with self._lock:
                    self._threads.discard(me)
                return
            job.run()
            del job

    def wait(self, done: concurrent.futures.Future[Any],
             start: Callable[[], None] = lambda: None) -> None:
        """Wait for `done` on a thread of this pool, which counts as
        not running meanwhile. `start` begins what completes `done`."""
        with self._lock:
            self._waiting += 1
            if not self._queue.empty():
                self._grow()
        try:
            start()
            concurrent.futures.wait([done])
        finally:
            with self._lock:
                self._waiting -= 1

    def shutdown(self, wait: bool = True, *,
                 cancel_futures: bool = False) -> None:
        with self._lock:
            self._shutdown = True
            threads = list(self._threads)
        self._queue.put(None)
        if wait:
            for thread in threads:
                if thread is not threading.current_thread():
                    thread.join()


# Every live pool, stopped before the interpreter joins its threads:
# `concurrent.futures.thread` stops its own the same way, and an
# `atexit` hook would run after that join.
_POOLS: weakref.WeakSet[_Pool] = weakref.WeakSet()


def _stop_pools() -> None:
    for pool in list(_POOLS):
        pool.shutdown(wait=True)


threading._register_atexit(_stop_pools)  # type: ignore[attr-defined]


class _Home(concurrent.futures.Executor):
    """One thread with the evaluator's stack, whose work can also run
    while that thread waits (`wait`).

    A worker waits so for a hook that the loop answers (huggorm#155).
    Work submitted during the wait runs on the waiting thread, as a C++
    call stack runs a nested call. Work queued before the wait stays
    queued, so it still runs after the call that waits."""

    def __init__(self, name: str) -> None:
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=name)
        _start_with_eval_stack(self._pool)
        self._lock = threading.Lock()
        self._inbox: queue.SimpleQueue[_Job | None] | None = None

    def submit(self, fn: Callable[..., Any], /, *args: Any,
               **kwargs: Any) -> concurrent.futures.Future[Any]:
        with self._lock:
            if self._inbox is None:
                return self._pool.submit(fn, *args, **kwargs)
            job = _Job(functools.partial(fn, *args, **kwargs))
            self._inbox.put(job)
            return job.future

    def shutdown(self, wait: bool = True, *,
                 cancel_futures: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    def wait(self, done: concurrent.futures.Future[Any],
             start: Callable[[], None] = lambda: None) -> None:
        """Run what is submitted here until `done` completes. Only on
        this executor's own thread, from inside a job it runs.

        `start` runs once the inbox takes work. Whatever completes
        `done` begins there, so a submit it causes cannot reach the
        queue behind this job."""
        inbox: queue.SimpleQueue[_Job | None] = queue.SimpleQueue()
        with self._lock:
            outer, self._inbox = self._inbox, inbox
        done.add_done_callback(lambda _: inbox.put(None))
        try:
            start()
            while (job := inbox.get()) is not None:
                job.run()
        finally:
            with self._lock:
                self._inbox = outer
                # A submit after the wake and before the line above.
                while True:
                    try:
                        left = inbox.get_nowait()
                    except queue.Empty:
                        break
                    if left is None:
                        continue
                    if outer is not None:
                        outer.put(left)
                    else:
                        self._pool.submit(left.run)


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


# The hooks of the call this thread runs, for a stub Nix calls here.
_CALLING = threading.local()


# How many `@posted` hooks one call may have queued and not yet run. A
# count, not bytes: a NAR piece is what fills `SinkBuffer`'s 32 KiB, or
# one larger write Nix makes whole. An accessor with no chunked
# `readFile` (dummy://, an in-memory tree) writes each file whole.
_POSTED_WINDOW = 16


class _Hooks:
    """The async hooks one call's worker asks its awaiting coroutine to
    run, through a stub Nix calls (huggorm#155).

    The coroutine runs each on the loop, inside the caller's task, so a
    cancel reaches the hook. The worker waits for the answer through
    its executor: a `_Home` runs new work on it meanwhile, so a hook may
    call the same object back, and the `_Pool` starts a thread in its
    place, so a hook may use the pool.

    The worker and the loop share `_asked` under `_lock`. The worker
    wakes the loop only when `_awake` is off, and the loop turns it off
    only when it finds `_asked` empty, under the same lock. So a run of
    posted hooks costs one hop to the loop, not one each."""

    __slots__ = ("_asked", "_awake", "_broken", "_closed", "_lock",
                 "_posted", "_token", "executor", "wake")

    def __init__(self, executor: concurrent.futures.Executor) -> None:
        self._token = anyio.lowlevel.current_token()
        self.executor = (executor if isinstance(executor, _Home | _Pool)
                         else None)
        self._lock = threading.Lock()
        self._asked: collections.deque[
            tuple[Callable[[], Awaitable[Any]],
                  concurrent.futures.Future[Any], bool]] = collections.deque()
        self._awake = False
        self._closed = False
        # The first posted hook that failed. The ones after it do not
        # run: a stream with a gap is worse than one that stops.
        self._broken: Exception | None = None
        # Worker side: the answers of posted hooks, oldest first.
        self._posted: collections.deque[
            concurrent.futures.Future[Any]] = collections.deque()
        self.wake = anyio.Event()

    def ask(self, hook: Callable[[], Awaitable[Any]]) -> Any:
        """From the worker: what `hook` answers on the loop."""
        answer: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._wait(answer, lambda: self._put(hook, answer, posted=False))
        return answer.result()

    def post(self, hook: Callable[[], Awaitable[Any]]) -> None:
        """From the worker: queue `hook` and go on. A full window waits
        for the oldest. A failure raises here, at a later post, or at
        `flush`."""
        if isinstance(self.executor, _Home):
            # A posted hook runs while this thread works rather than
            # waits, so a call it makes to this thread queues behind the
            # call that waits for the hook, and neither ends.
            self.ask(hook)
            return
        while self._posted and (self._posted[0].done()
                                or len(self._posted) >= _POSTED_WINDOW):
            self._wait(self._posted[0])
            self._posted.popleft().result()
        answer: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._put(hook, answer, posted=True)
        self._posted.append(answer)

    def flush(self) -> BaseException | None:
        """From the worker, as its call ends: wait until every posted
        hook has run, and answer the first failure."""
        failure: BaseException | None = None
        while self._posted:
            self._wait(self._posted[0])
            done = self._posted.popleft()
            if failure is None:
                failure = done.exception()
        return failure

    def _wait(self, done: concurrent.futures.Future[Any],
              start: Callable[[], None] = lambda: None) -> None:
        if self.executor is not None:
            self.executor.wait(done, start)
        else:
            start()
            concurrent.futures.wait([done])

    def _put(self, hook: Callable[[], Awaitable[Any]],
             answer: concurrent.futures.Future[Any], posted: bool) -> None:
        with self._lock:
            if self._closed:
                answer.set_exception(_cancelled())
                return
            self._asked.append((hook, answer, posted))
            if self._awake:
                return
            self._awake = True
        anyio.from_thread.run_sync(self._wake, token=self._token)

    def _wake(self) -> None:
        # Reads `wake` on the loop: `_until_done` replaces it there.
        self.wake.set()

    async def run_asked(self) -> None:
        """Run every hook asked so far, in order."""
        while True:
            with self._lock:
                if not self._asked:
                    self._awake = False
                    return
                hook, answer, posted = self._asked.popleft()
            if posted and self._broken is not None:
                answer.set_exception(self._broken)
                continue
            try:
                result = await hook()
            except Exception as e:
                if posted:
                    self._broken = e
                answer.set_exception(e)
            except BaseException:
                answer.set_exception(_cancelled())
                raise
            else:
                answer.set_result(result)

    def close(self) -> None:
        """Fail every hook asked, and every one asked later: the
        coroutine that runs them was cancelled."""
        with self._lock:
            self._closed = True
            asked, self._asked = self._asked, collections.deque()
        for _, answer, _ in asked:
            answer.set_exception(_cancelled())


def _cancelled() -> RuntimeError:
    return RuntimeError("the call that asked for this hook was cancelled")


def adapt(base: type, obj: Any) -> Any:
    """`obj` as Nix can call it: itself when it is a `base`, else a
    stub whose hooks run `obj`'s async methods (huggorm#155)."""
    if obj is None or isinstance(obj, base):
        return obj
    return _stub_class(base)(obj)


@functools.cache
def _stub_class(base: type) -> type:
    """A subclass of `base` whose every hook asks the running call. A
    class attribute per hook, because nanobind's trampoline finds an
    override on the type."""
    # By name: this file is a payload, and `_policy` exists only beside
    # the copy the build writes.
    callbacks = importlib.import_module(f"{__package__}._policy").CALLBACKS

    def __init__(self: Any, target: Any) -> None:
        base.__init__(self)
        self._target = target

    def hook(name: str, posted: bool) -> Callable[..., Any]:
        def call(self: Any, *args: Any) -> Any:
            hooks: _Hooks | None = getattr(_CALLING, "hooks", None)
            if hooks is None:
                raise RuntimeError(
                    f"{name} is an async hook, and no huggorm call runs on "
                    f"this thread to run it from")
            run = functools.partial(getattr(self._target, name), *args)
            if posted:
                hooks.post(run)
                return None
            return hooks.ask(run)
        call.__name__ = name
        return call

    namespace = {"__init__": __init__,
                 **{h.call.name: hook(h.call.name, h.posted)
                    for h in callbacks[base.__name__]}}
    return types.new_class(f"Hooked{base.__name__}", (base,), {},
                           lambda ns: ns.update(namespace))


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

    __slots__ = ("_hooks", "_outer", "_previous", "_request")

    def __init__(self, request: int, hooks: _Hooks | None = None) -> None:
        self._request = request
        self._previous = 0
        self._hooks = hooks
        self._outer: _Hooks | None = None

    def __enter__(self) -> None:
        import huggorm_bindings

        self._previous = huggorm_bindings.begin_request(self._request)
        self._outer = getattr(_CALLING, "hooks", None)
        _CALLING.hooks = self._hooks

    def __exit__(self, kind: object, *exc: object) -> None:
        import huggorm_bindings

        try:
            # The call ends only once its posted hooks have run.
            failure = (self._hooks.flush() if self._hooks is not None
                       else None)
            if failure is not None and kind is None:
                raise failure
        finally:
            _CALLING.hooks = self._outer
            # Pushes the "finalized" marker for the id that is ending, and
            # puts back whatever this thread was inside before.
            huggorm_bindings.end_request(self._previous)


async def _until_done(future: asyncio.Future[Any], request: int,
                      hooks: _Hooks) -> Any:
    """Await work on an executor thread, and stop it when cancelled.

    Cancelling the awaiting task does not stop a thread that is
    already running, and cancelling `future` would only detach it. So
    this never cancels the future. It waits on an event the future
    sets, and on cancellation it marks the request cancelled, which
    Nix sees at its next `checkInterrupt` on that thread.

    Then it waits again, shielded, until the thread has stopped. A
    caller whose cancellation returned while the evaluator still ran
    would queue its next call behind that work. The outcome of the
    stopped work is discarded: the caller was cancelled, and the
    cancellation is what it gets.

    Meanwhile it runs every async hook the worker asks for. A cancel
    fails the hook that runs and refuses the next, so a worker that
    waits for one stops too, and the shielded wait ends."""
    future.add_done_callback(lambda _: hooks.wake.set())
    try:
        while not future.done():
            await hooks.wake.wait()
            hooks.wake = anyio.Event()
            await hooks.run_asked()
    except BaseException:
        import huggorm_bindings

        hooks.close()
        huggorm_bindings.cancel_request(request)
        try:
            with anyio.CancelScope(shield=True):
                while not future.done():
                    await hooks.wake.wait()
                    hooks.wake = anyio.Event()
            # Discarded, so read: asyncio logs a failure nothing read.
            if not future.cancelled():
                future.exception()
        finally:
            huggorm_bindings.forget_request(request)
        raise
    return future.result()


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
    # Every wrapper the build emits states `_copied`, so a missing one
    # is an AttributeError rather than a proxy by default.
    if x._copied:
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
            # A protocol-typed parameter admits a remote handle
            # statically, so the location is checked here (huggorm#26).
            if hasattr(x, "handle_id"):
                raise TypeError(
                    f"{type(x).__name__} is a handle on a server, and an "
                    f"in-process call needs the object itself")
            continue
        if x._copied:
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
            # is not read as covered (huggorm#75's lesson).
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

    @classmethod
    def adopt(cls, obj: Any, parent: BaseRunner) -> BaseRunner:
        """A runner of this kind over `obj`, which `parent`'s call
        produced. The build picks the class from the produced type's
        declared execution."""
        return cls(None, obj=obj)

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

    def _invoke(self, method: str, args: list[Any],
                request: int | None, hooks: _Hooks | None = None) -> Any:
        try:
            with (_InRequest(request, hooks) if request is not None
                  else contextlib.nullcontext()):
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

    def _executor(self) -> concurrent.futures.Executor:
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
        loop = asyncio.get_running_loop()
        request = _next_request()
        hooks = _Hooks(self._executor())

        def invoke() -> Any:
            try:
                with _InRequest(request, hooks):
                    return fn(self._resolve())
            except Exception as e:
                if hasattr(e, "to_dict"):
                    raise
                raise InternalError("run failed", cause=e) from e

        return await _until_done(
            loop.run_in_executor(self._executor(), invoke), request, hooks)

    async def call(self, method: str, args: list[Any]) -> Any:
        _check_isolation(self, args)
        # A dedicated thread runs its calls in the order they arrive here,
        # so nothing may await before the submit (huggorm#155). Its
        # arguments build in `_invoke` instead: a pool object builds on
        # any thread, and `_check_isolation` refused every affine one
        # that lives on another thread.
        if not self.dedicated_thread:
            await _materialize_args(args)
        loop = asyncio.get_running_loop()
        # Allocated HERE, on the loop thread, and passed in.
        # run_in_executor carries no context, so a contextvar set on
        # this side would not reach the thread that does the work.
        request = _next_request()
        hooks = _Hooks(self._executor())
        return await _until_done(loop.run_in_executor(
            self._executor(),
            lambda: self._invoke(method, args, request, hooks)),
            request, hooks)


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
        self._pool = _Home(name)

    def _executor(self) -> concurrent.futures.Executor:
        return self._pool

    @classmethod
    def adopt(cls, obj: Any, parent: BaseRunner) -> BaseRunner:
        """An affine object stays on the thread that made it, so it
        runs on its producer's executor."""
        if isinstance(parent, AffineRunner | AttachedRunner):
            return AttachedRunner(obj, parent)
        raise TypeError(
            "affine return type produced on a pool runner: the object has "
            "no home thread. Declare it 'pool' or produce it from an "
            "affine wrapper."
        )

    async def aclose(self) -> None:
        # The dedicated thread is about to die, so it has to leave the
        # collector's list first - on itself, as its last GC action.
        # Skipping that left a dead thread registered, and the next
        # collection aborted the whole process (the server's reaper
        # closes affine wrappers, so this reached production paths).
        # Submitted BEFORE shutdown, so it is the last work this
        # single-worker executor accepts and runs. It drops the object
        # first, so a C++ destructor runs on its own thread too.
        self._pool.submit(self._retire)
        # The wait lasts as long as the work still queued, which is an
        # evaluation when a server closes a dropped state. On the loop
        # it stalled every other client (huggorm#143).
        await anyio.to_thread.run_sync(lambda: self._pool.shutdown(wait=True))

    def _retire(self) -> None:
        self._obj = None
        _release_gc_thread()


class PoolRunner(BaseRunner):
    """Operations run on a shared pool. Target must be thread-safe."""

    def _executor(self) -> concurrent.futures.Executor:
        return _shared_pool()

    async def aclose(self) -> None:
        return None  # shared pool outlives wrappers


class InlineRunner(BaseRunner):
    """Operations run on the calling thread, with no request.

    For a pool class none of whose methods can wait. A hop would buy
    neither a home thread nor a released GIL. A request id would make
    `end_request` push a "finalized" marker from a pool thread, which
    has no queue, so the marker falls through to the process queue -
    one per call, and a log drain calls every `LOG_POLL`."""

    async def call(self, method: str, args: list[Any]) -> Any:
        _check_isolation(self, args)
        await _materialize_args(args)
        return self._invoke(method, args, None)

    async def run(self, fn: Callable[[Any], Any]) -> Any:
        return fn(self.ensure())

    async def materialize(self) -> None:
        self._resolve()

    async def aclose(self) -> None:
        return None


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

    def _executor(self) -> concurrent.futures.Executor:
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
    hooks = _Hooks(_shared_pool())

    def invoke() -> Any:
        try:
            with _InRequest(request, hooks):
                return fn(*[unwrap_arg(a) for a in args])
        except Exception as e:
            if hasattr(e, "to_dict"):
                raise  # typed wrapper error - pass through untouched
            raise InternalError(f"{fn.__name__} failed", cause=e) from e

    return await _until_done(
        loop.run_in_executor(_shared_pool(), invoke), request, hooks)

