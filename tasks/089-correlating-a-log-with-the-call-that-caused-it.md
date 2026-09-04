# Correlating a log with the call that caused it

**OPEN.** A reflection, not a plan yet. Carl asked what nanopynix
does about a per-request log id and about granular verbosity, and
how either fits here. This is the answer, the parts of it that were
verified against Nix's own source, and the two decisions it forces.

Nothing is implemented. The C++ half needs a per-occasion ask.

## What nanopynix does

Read from `~/Code/nanopynix` at 2026-09-04.

### A request id, thread-local, set around every dispatch

`nix_util.cpp:95` holds `static thread_local int64_t
logger_request_id`, and every one of `PyLogger`'s five methods passes
it as the FIRST argument of the callback:

    _cb(nb::int_(logger_request_id), "msg", int(lvl), std::string(s));

The comment says why a thread-local rather than a member: "Nix
invokes Logger callbacks on the originating Nix thread. A request ID
is therefore thread-local operation context, not mutable global
logger state."

Python sets it at ONE chokepoint. `inproc/_impl.py:246`,
`_run_with_log_context(operation_id, verbosity, func, args)`, saves
both thread-locals, sets both, calls, and restores both in a
`finally`. Every in-process Nix call goes through it, and the ids
come from one allocator (`_next_operation_id`) so a capture can tag
what it started.

### A finalized marker, which is the part that makes it usable

An id alone says which call a record belongs to. It does not say when
that call's records have stopped. So the dispatch site's `finally`
enqueues `request_finalized(operation_id)`, and that marker is
CONTROL rather than log:

> A lost `request_finalized` marker is not a lost log line. It parks
> `LogCapture.wait` until that wait's own bound expires, and it makes
> the capture report itself incomplete. So a control event takes the
> place of the oldest queued event rather than joining it in the drop
> count.

That is the whole design in one rule, and their module docstring
states it: "A log event may be lost. Nix's progress may not be
delayed. Exactly one hop in each process is lossy, it sits at the
process boundary, and every hop above it is guaranteed to drain. A
control event is never lost."

### Verbosity, per thread, with the global pinned open

`nix_util.cpp:176` holds `static thread_local nix::Verbosity
thread_verbosity`, defaulted from a `std::atomic<int>
default_verbosity` because Nix starts threads that never pass the
dispatch wrapper.

`nix::verbosity` itself is written ONCE at import and pinned wide
open. Their reason is a data race, not a preference: it is a plain
non-atomic global that every call site reads on its own thread while
a caller writes it from another, ThreadSanitizer reports it, and
patching a public header hundreds of sites read is not an option.

The price is that Nix's own gate stops rejecting anything, so every
site at or under the pinned ceiling formats its message before the
logger drops it. They measured the ceiling and chose `chatty`.

Ownership is per OBJECT, and `protocols.py:772` states it: an
evaluator owns its level, two evaluators of one session can hold
different levels at once, an evaluator follows its session until it
sets one and never follows again after. Store work stays at the
session's level. Nix's own threads read the process default.

### Fan-out, in Python, above one C++ callback

`CallbackBus` is a list of callbacks: one C++ callback in, N
subscribers out, and zero subscribers costs nothing. The worker side
is deliberately NOT built on it, because that side has to serialize
to protobuf.

## What was verified here, against Nix 2.34.8

Their numbers are their measurement on their version. Goal 1 says not
to trust a memory of Nix, so the structural claims were re-read in
`/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source`:

- `printMsg` gates on `__lvl <= nix::verbosity`, in a macro, so the
  arguments are lazy and a rejected message costs nothing
  (`logging.hh:327`). CONFIRMED - so pinning the global open really
  does move the cost to every call site.
- `Activity::Activity` calls `logger.startActivity(...)`
  unconditionally in its body (`logging.cc:194`). CONFIRMED - so the
  high-volume path has no gate to lose, which is the load-bearing
  half of "the levels at or under chatty are free".
- 138 `debug()` sites in libstore against their "139". 228 tree-wide
  against their "208", and 5 `vomit()` against their 5. Close, and
  the difference is version and counting, not a different world.
- libexpr holds 4 literal `printMsg(` and 29 sites across the whole
  macro family. They said 19. Either number supports the same claim:
  the evaluation hot loop is not where the logging is.

**Their timing table is NOT adopted.** `chatty` being the highest free
ceiling was measured on their workloads, through their engines, on
2.31 for the flake case. It has to be re-measured here before it
becomes a constant in this repo.

## The conflict this repo has and nanopynix does not

**huggorm's tap is a TEE, and nanopynix's logger is a replacement.**

`install_log_tap` uses `makeTeeLogger`, keeping the logger that was
there as the MAIN one - so a console user still sees output and the
binding writes no forwarding of its own. `PyLogger` instead checks
`thread_verbosity` before BOTH the callback and its fallback, so its
filter covers every path.

Pin `nix::verbosity` open here and the console path loses its filter:
Nix would hand every chatty-level message to the tee, our arm would
drop what the thread did not ask for, and the OTHER arm would print
all of it. A caller who asked for less would get more.

So adopting per-thread verbosity needs one of:

1. the tap becomes the filter for both arms - it stops being a tee
   and starts forwarding to the previous logger itself, which is
   exactly the "this file writes no forwarding of its own" that
   `install_log_tap`'s comment currently claims as a virtue;
2. the global is not pinned, and per-thread verbosity can only NARROW
   what the global admits - which is what `subscribe_logs` already
   documents, and which means `debug` is unreachable without raising
   the global for everyone;
3. no per-thread verbosity at all, and `LogQueue.level` stays the
   only filter.

**This is the decision to make first.** Everything else follows it.

## Two filters on one axis

`LogQueue.level` already filters a `"msg"` by level, and it is
documented as being able to NARROW only, because the global filters
first. A per-thread verbosity would be a second filter on the same
axis. Goal 3 says one of them owns it.

Under option 2 above they compose and both are honest. Under option 1
the queue's level becomes redundant with the thread's, and the
"narrow only" sentence in `subscribe_logs` becomes false - the thread
level would be the authority and could widen.

## Fan-out needs no C++, and Carl's message makes it required

`tasks/085` gap 3 argued fan-out was "a plausible want and not an
observed one". That rationale is REFUTED, by Carl:

> this also pretty much means we have to support multiple readers
> since a CLI would want a global listener that prints to
> stdout/stderr as things happen

Recorded rather than quietly changed, because the gap's own argument
is what it overturns.

nanopynix shows the shape, and it is all Python: one C++ queue, one
drainer, a bus above it, N subscribers, zero subscribers free. That
maps onto what this repo already has with no binding change - the
`LogQueue` and the single-reader `subscribe_logs` stay exactly as
shipped, and the bus goes above them. It also supersedes
`Session/ProcessLogs`'s one-reader-per-server refusal, at the async
layer Carl already designated as the enforcement point for the state
isolation.

## What a request id would and would NOT buy here

The chokepoint exists already: `BaseRunner.call` and `_invoke`, the
same place the isolation check went, and the same shape as
`_run_with_log_context`.

What it does not cover is the part this repo just built. nanopynix's
own comment says Nix's threads - a substituter, a build hook reader -
"never pass through the dispatch wrapper", so their records carry id
0. Those are exactly the records `subscribe_process_logs` exists for.
So a request id correlates the EVALUATION thread and says nothing
about a build's output.

The cross-thread half is already on the record and is not an id: a
`"start"` carries `parent`, so an activity raised on a fetcher thread
is reachable from the activity that caused it. Say this plainly or
the feature over-promises.

A `request_finalized` marker would be new here, and it is the half
that makes an id worth having: without it a reader knows which call a
record belongs to and never knows the call is done.

## What it would cost

**C++, and it needs a per-occasion ask.** About ten lines in
`eval.hpp`: a `thread_local int64_t`, a setter and a getter, and a
field on `LogRecord`. Plus the declaration for the two accessors, and
one more `@reads` on `LogRecord`.

Per-thread verbosity is more: the pinning decision above, a second
thread-local, an atomic default, and the tee question.

**Python.** The bus, one drainer per queue, and the id set and
restored in `BaseRunner.call`. The finalized marker needs a shape the
`LogQueue` does not have - a control event that cannot be dropped -
which is the one place nanopynix's design does not fit this repo's
queue as it stands, because `LogQueue` drops by ACTION and has no
priority arm.

## The order to do it in

1. Decide the tee-versus-pin question. Nothing else is safe first.
2. Fan-out, which is 085 gap 3, needs no C++ and is required by the
   CLI use Carl named.
3. The request id and the finalized marker, together - an id without
   a marker is half a feature.
4. Per-thread verbosity, last, and only after the ceiling is
   re-measured on this repo's workloads rather than adopted.

Opened 2026-09-04, from Carl's question.
