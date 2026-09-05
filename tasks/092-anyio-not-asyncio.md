# anyio, not asyncio

**OPEN.** Carl, 2026-09-05:

> We should be using anyio primitives instead of asyncio to the
> greatest extent possible (preferably only), record this in
> CLAUDE.md and work on it. anyio's structured async model is good at
> preventing bugs.

The rule is in `CLAUDE.md`. This file holds the survey, the one
exception and the reading behind it, and what each conversion buys.

## What is there today

Every `asyncio` name outside the tests, by file.

    server.py     Lock, sleep x2, ensure_future x2, create_task,
                  Task[None], CancelledError x2, run
    remote.py     create_task, sleep, wait_for x2, Task[None]
    watch.py      Lock
    runtime.py    get_running_loop x4, run_in_executor x4
    smoke_test.py gather x4, get_running_loop x2, run
    notify.py     named in a comment only

## The exception, and it is measured

`runtime.py` keeps `loop.run_in_executor`. Read
`anyio/_backends/_asyncio.py`, `run_sync_in_worker_thread`:

    if not idle_workers:
        worker = WorkerThread(root_task, workers, idle_workers)
        ...
    else:
        worker = idle_workers.pop()
        # Prune any other workers that have been idle for
        # MAX_IDLE_TIME seconds or longer

A SHARED pool, LIFO, with expiry. `run_sync` takes a `limiter` and no
thread. So there is no way to say "this call, on that thread".

Two things depend on exactly that:

- An `EvalState` is AFFINE. `AffineRunner` gives each one a
  `ThreadPoolExecutor(max_workers=1)`, and `thread_queue` in
  `logging.hpp` is a `thread_local` that assumes it.
- `_refuse_foreign` compares `callee._executor() is arg._executor()`.
  That identity check IS the isolation Carl ruled on: values from one
  `EvalState` are not valid for another.

### The anyio-native shape that was rejected

It is constructible. One long-lived
`to_thread.run_sync(worker_loop, limiter=CapacityLimiter(1))` per
state, consuming a job queue, gives an affine thread with an anyio
spelling.

Rejected. It rebuilds `run_in_executor` by hand, it replaces the
executor identity that `_refuse_foreign` reads with something else to
compare, and it buys NO structure - an affine thread already has one
owner and one lifetime, which is the whole thing a task group would
have added.

### The shared pool stays too, for a weaker reason

`_shared_pool` needs no affinity, so `to_thread.run_sync` with a
`CapacityLimiter(_POOL_SIZE)` would work. It is not done because it
buys nothing and it is not free: the pool would churn where it does
not today, and this repo has already aborted a process over thread
churn under Boehm (`gc.hpp`, "verified by reproducing the abort
first"). The `thread_local` destructor makes churn CORRECT rather than
leaky, so this is a preference and not a defect - recorded so the next
reader does not re-derive it.

## What the conversion buys, per site

    server.py   _detach + the retained `self._closing` set + the
                "asyncio holds only a weak reference to a running
                task" comment ALL die. A task group holds its
                children. This is the concrete instance of Carl's
                point.
    server.py   `_Fanout` stops holding a Task and cancelling it.
    remote.py   the ping loop stops being a task the client has to
                remember to cancel.
    both        `wait_for` -> `fail_after`, which is a cancel scope
                and composes.

## Three traps, named before they are hit

1. **A task group WAITS for its children; it does not cancel them.**
   `sweeper()` and `_Fanout._run` loop forever, so a task group around
   `serve()` hangs on shutdown unless the exit cancels the scope.
2. **`start_soon` hands back no task.** `_Fanout.leave` cancels the
   drain and awaits it before unsubscribing, and that ordering is
   GATED (`test_the_last_reader_out_unsubscribes`). It has to become a
   `CancelScope` stored on the fan-out plus an `anyio.Event` the drain
   sets in its `finally`.
3. **Never swallow the cancellation exception.**
   `contextlib.suppress(asyncio.CancelledError, Exception)` in
   `_Fanout.leave` is hostile under anyio's contract. Where one must
   be named, `anyio.get_cancelled_exc_class()` is the spelling.

## Order

1. This file and the `CLAUDE.md` rule.
2. The mechanical swaps: `Lock`, `sleep`, `wait_for`, `gather`, `run`.
3. `server.py`'s task groups, which is the structural one.
4. `remote.py`'s ping loop.

Opened 2026-09-05.
