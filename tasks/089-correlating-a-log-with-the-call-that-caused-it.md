# Correlating a log with the call that caused it

**OPEN.** A reflection first, and now one decision. Carl asked what
nanopynix does about a per-request log id and about granular
verbosity, and how either fits here. This is the answer, the parts
of it that were verified against Nix's own source, and the question
it forced.

**The question is answered: REPLACE the logger, do not tee.** Carl
decided it, for the stdio transport, and the last section holds what
that costs. Everything else in this file is still a reflection.

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

**This was the decision to make first, and it is made: option 1.**
See the last section. Options 2 and 3 are closed.

## Two filters on one axis

`LogQueue.level` already filters a `"msg"` by level, and it is
documented as being able to NARROW only, because the global filters
first. A per-thread verbosity would be a second filter on the same
axis. Goal 3 says one of them owns it.

Under option 2 they would compose and both stay honest. Option 1 is
what was chosen, so the queue's level becomes redundant with the
thread's, and the "narrow only" sentence in `subscribe_logs` becomes
FALSE - the thread level is the authority and can widen. That
sentence has to change in the same commit as the pinning, or the
declaration will be documenting the opposite of what happens.

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

## Decided: REPLACE the logger, 2026-09-04

Carl:

> We should replace the logger, at some point we will want to be able
> to run the protocol over stdin/stdout and then it's important that
> we don't log there. I have code for grpclib that transports over
> various transports.

So option 1 above, and options 2 and 3 are closed. The tee goes.

### What replacing costs, read rather than assumed

Four things a tee currently gives that a replacement has to answer
for. Each was checked against 2.34.8 in
`/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source`.

**Console output.** `nix::logger` is initialised at static-init to
`makeSimpleLogger(true)` (`logging.cc:35`), and `SimpleLogger::log`
ends in `writeToStderr` - so today's tee prints to STDERR, not
stdout. Replacing it removes that unless the tap carries a fallback,
which is what nanopynix does (`_fallback{nix::makeSimpleLogger()}`).
Keep one, and keep it pointed at stderr.

**`writeToStdout`.** The base writes to descriptor 1 directly -
`getStandardOutput()` then `writeFull` (`logging.cc:42`) - and THAT
is the call that would corrupt an H2 frame. Every caller in 2.34.8 is
in `libcmd`, `libmain` or `src/nix`: the repl, `--json` output,
`nix develop`. None is in libexpr or libstore, so nothing huggorm
drives reaches it today. Override it anyway: "no caller today" is a
fact about one version, and the cost is three lines.

**`ask`.** The base returns `{}` already (`logging.hh:98`), and
nothing outside the tee logger calls it. Replacing loses nothing.

**`isVerbose`, `pause`, `resume`, `stop`, `setPrintBuildLogs`.** All
have empty or trivial base implementations. Nothing to preserve.

### The transport already defends descriptor 1, and that changes the argument

`grpclib-transports`' `take_wire_descriptors` (read at
`~/Code/nanopynix/grpclib-transports/src/grpclib_transports/stdio.py`)
moves the pipe pair off 0 and 1 before anything writes, then makes
descriptor 1 a duplicate of descriptor 2 and descriptor 0
`/dev/null`. Its own comment says why, and it is the same problem
from the other side:

> A redirection of `sys.stdout` is a Python-level rebinding, so it
> cannot stop a C++ library, a C extension or a subprocess from
> writing to the descriptor itself. Every such byte becomes an
> HTTP/2 frame, and the peer reports a protocol error that names
> nothing about where the byte came from.

So a stray write from libnix lands on stderr as a log line, not on
the wire as a corrupt frame, whatever the logger does.

That is worth stating precisely, because it means **stdout safety is
not the argument for replacing.** The transport has that covered
structurally, and it is the better place for it: it defends against a
subprocess and a C extension too, which no logger can.

The argument that survives is OWNERSHIP, and it is the stronger one:

- **The filter becomes ours.** This is what the conflict above was
  about. A tee leaves `SimpleLogger` filtering on the global
  `nix::verbosity` with no way for a caller to narrow it, so
  per-thread verbosity is unimplementable while the tee stands.
  Replace, and the tap is the only reader of the level - which makes
  pinning the global open safe, and makes option 1 the same as this
  decision.
- **The destination is a service.** A daemon that writes to a
  descriptor nobody reads is writing into a pipe buffer that fills.
  Where Nix's output goes is a policy this project should state
  rather than inherit.

### The shape, and what it needs

    inline void install_log_tap()
    {
        nix::logger = std::make_unique<LogTap>();
    }

plus, on `LogTap`:

- a `SimpleLogger` fallback, used when no queue takes the record, so
  a console user still sees Nix. Pointed at stderr, which is where
  `SimpleLogger` already writes;
- a `writeToStdout` override, so descriptor 1 is never written by
  libnix through us;
- and later, the per-thread verbosity check, which is the whole
  reason the tee had to go.

`install_log_tap`'s current comment claims the tee as a virtue - "so
this file writes no forwarding of its own". That sentence becomes
false and has to go with it.

**NOT WRITTEN YET.** It is C++ in `cpp/eval.hpp` and needs Carl's
per-occasion approval, which is asked for separately.

### One thing this does not change

`nix::logger` is still replaced ONCE, at import, on the main thread,
before any Nix thread exists. That is not a tee property, it is a
race property, and both this repo's comment and nanopynix's give the
same reason: it is a plain global `unique_ptr`, and replacing it
while another thread reads it has no lock to take.

nanopynix goes one step further and never frees theirs, because
ThreadSanitizer caught a curl worker thread reading the logger
through `Activity::~Activity` after a session freed it - and
`curlFileTransfer` starts that thread in its own constructor and
offers no way to join it. A tap installed once and never replaced has
the same property for free, so this repo does not need their
leaked-singleton trick as long as nothing ever swaps it back.

### First: the C++ moves out of `eval.hpp`

Carl, when the replacement was put to him:

> this doesn't belong in eval.hpp, there's no point in limiting the
> amount of files, separate as appropriate.

Right, and `eval.hpp` had grown to 1289 lines holding four unrelated
things: a reach into libexpr's private caches, the log tap, the
collector, and the evaluator itself.

Two moved, and they were the two that could - neither the tap nor
the collector depends on the other, and neither depends on the
evaluator:

    eval.hpp     1289 -> 754
    logging.hpp            440
    gc.hpp                 170

The evaluator DOES depend on the collector, because `alloc` and every
`Bridge` register a thread, so `eval.hpp` includes `gc.hpp` rather
than merely sitting beside it. The reach into the private caches
stays with the evaluator: `cached_files` and `forget_file` take an
`EvalState` and are about nothing else.

A MOVE, and verified as one. The extracted text is the file's own
lines sliced at the section banners, nothing retyped, and the emitted
C++ differs by exactly two lines:

    > #include "huggorm_decl/cpp/gc.hpp"
    > #include "huggorm_decl/cpp/logging.hpp"

which is `@header` and `@needs` in the declaration pointing at where
the code went. Three classes and six free functions were retargeted
BY LINE, because `@header("huggorm_decl/cpp/eval.hpp")` is the same
string in five places and only three of them move.

**The includes go ABOVE `namespace huggorm {`, and that is not
style.** Put where the code used to be - inside it - they nest, and
every name in them becomes `huggorm::huggorm::`. The compiler said
so:

    error: 'install_log_tap' is not a member of 'huggorm'; did you
    mean 'huggorm::huggorm::install_log_tap'?

Worth recording because the sections were self-contained in every
other respect, and the one thing that was not self-contained was the
namespace they had been sitting inside.
