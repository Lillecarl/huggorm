# anyio, not asyncio

**MOSTLY DONE.** Carl, 2026-09-05:

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

## Done, 2026-09-05, except one spawn

Every `asyncio` name outside `runtime.py` is gone but one.

    watch.py       Lock            -> anyio.Lock
    server.py      Lock, sleep     -> anyio
    server.py      ensure_future   -> two task groups
    server.py      create_task     -> loops.start_soon
    server.py      run             -> anyio.run
    smoke_test.py  gather x4       -> `_all` / `_all_errors`
    smoke_test.py  loop().time()   -> anyio.current_time
    smoke_test.py  run             -> anyio.run
    remote.py      sleep, wait_for -> anyio.sleep, anyio.fail_after
    remote.py      create_task     -> STILL asyncio, see below

### The two task groups in `serve`

`work` is outer and AWAITED. It holds the runner shutdowns `_on_drop`
starts, and each of those releases an affine thread from the
collector's list - work that must finish rather than be cancelled.

`loops` is inner and CANCELLED. It holds the sweeper and the log
drains, which never return on their own.

Being inner is what orders them: the loops stop first, and anything
they started is still awaited by `work` afterwards. This is trap 1
above, answered.

`_detach`, the retained `self._closing` set, and the comment
explaining that "asyncio holds only a weak reference to a running
task" are all GONE. That is the concrete thing Carl's point buys
here: the workaround existed because nothing owned the task, and a
task group owns it.

`start_soon` is a plain method rather than a coroutine, which is what
lets `_on_drop` - a callback the sweep calls synchronously - still
start one.

### The bug the conversion caused, and the suite caught

`_Fanout._run` set `self._stopped` after its cancel scope exited, and
`leave` clears that attribute before the drain notices the cancel. So
the first run died at startup:

    File "huggorm/server.py", line 385, in _run
    AttributeError: 'NoneType' object has no attribute 'set'

The server never listened and 11 tests failed with
`ConnectionRefusedError`. Both `sub` and `stopped` are parameters
now, for the same reason `sub` already was.

### The teardown ORDER is argued, not gated

`leave` cancels the drain, waits for it, and only then unsubscribes.
Two perturbations were tried and NEITHER produced a failing test:

    drop `await stopped.wait()`     ruff refuses it - F841, `stopped`
                                    assigned and never used. So the
                                    linter holds the shape, and no
                                    test was reached.
    wait AFTER the unsubscribe      396 passed. The ordering is not
                                    gated.

The second one is the honest finding. `sub.close()` empties the queue
anyway, so a late drain finds a closed queue and the binding tolerates
the race. The wait stays because it makes the sequence say what it
means, not because a test proves it must.

Recorded rather than left implied: the three fan-out gates from
`tasks/085` still hold, and they are what proves the CancelScope and
Event replace the Task correctly.

### The one spawn left, and why it needs Carl

`NixClient._ping_loop` still starts with `asyncio.create_task`.

anyio starts a task only inside a task group, and a task group is a
scope somebody holds open. A `NixClient` has none: `connect()` builds
it and a synchronous `stop_pinging()` stops it, so there is no
`async with` to own the loop.

Giving the client one is the fix and it is a BREAKING change to a
surface other people use - the third audience in `CLAUDE.md`, and the
kind of change that file calls expensive. So it is Carl's call:

    async with await remote.connect(host, port) as client:
        ...

against keeping `connect()` and `stop_pinging()` working for a caller
that never enters a scope. The two cannot both be true, because a
client used outside the scope would have no pinger and would be swept.
