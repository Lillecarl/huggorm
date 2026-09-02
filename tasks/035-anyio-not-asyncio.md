# anyio, not asyncio

**OPEN.** The suite runs under anyio; the runtime does not.
`payload/runtime.py` still calls `loop.run_in_executor` in four places,
which is the half this file calls movable now.

Carl, 2026-08-25: "We should use anyio instead of asyncio where
possible, this includes anyio's test runner instead of pytest-asyncio.
This is what my other related projects do and what we should continue
doing. You don't have to migrate all code to anyio primitives straight
away but pytest-asyncio is to be removed in favour of anyio, it's more
correct."

## Done: the suites

The test harness is anyio throughout - the runner, the subprocess, the
port probe, the timeouts, the sleeps. pytest-asyncio is gone.

Two things about anyio's plugin worth knowing before touching it:

- `anyio_mode = "auto"` runs every async test under it without a
  marker, the same way `asyncio_mode` did. (I first wrote that anyio
  had no auto mode and marked every suite by hand; Carl corrected it.
  anyio has had the setting since 4.x.)
- the runner is cached at the `anyio_backend` fixture's scope. That
  fixture is session-scoped here on purpose, because that is what lets
  a server fixture outlive one test. A client belongs to the loop it
  connected on, and reconnecting is what several of these tests are
  about.

Structured concurrency changed one thing for the better. The server
fixture's log drain used to be a fire-and-forget task; it is now a
task group whose scope encloses the yield, so the reader cannot
outlive the fixture. That is the same class of bug as the reaper task
asyncio could collect before it ran (fixed in the server earlier), and
anyio makes it unrepresentable rather than merely fixed.

## Not done: the library

`remote.py`, `server.py` and the generated `_runtime.py` are still
asyncio, and one of them cannot move on its own: grpclib is an asyncio
library. So the migration splits in two.

**Movable now**, because none of it touches the transport:

- `_runtime.py`. `run_in_executor` becomes `anyio.to_thread.run_sync`,
  which also gets a limiter rather than a hand-rolled pool - though
  the AFFINE runner needs its own dedicated thread and anyio's thread
  pool does not offer thread affinity, so that one stays a
  `concurrent.futures` executor either way.
- the client's ping loop, which is a bare `asyncio.sleep` in a task
  that has to be cancelled by hand. A task group ends it by scope.

**Blocked on the transport**: anything that touches grpclib. Either it
keeps an asyncio loop underneath (anyio's asyncio backend runs on one,
so this works today and is what the suites already prove), or the
transport itself changes. That is a bigger decision than a primitive
swap and it belongs with tasks/014, which already asks what the
transport should be.

The honest order is: move the runtime and the ping loop, leave grpclib
alone, and revisit when 014 does.
