# Logs flow back to the client

**MOSTLY DONE.** The in-process half is built: a tap under
`nix::logger`, and a bounded queue a subscriber drains. What is left
is the rpc, which is a server-streaming one and is protocol rather
than generated surface. "Built, 2026-09-03" at the end says what
works, what building it found, and what is still open.

Carl, 2026-08-25: "Nix will emit logs on a callback that flows back to
the client."

## What upstream actually is

Verified against src/libutil/include/nix/util/logging.hh in
/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source. `nix::Logger` is
an abstract class with a global instance, and it is a bigger surface
than "a line of text":

    virtual void log(Verbosity lvl, std::string_view s) = 0;
    virtual void logEI(const ErrorInfo & ei) = 0;
    virtual void startActivity(ActivityId act, Verbosity lvl,
                               ActivityType type, const std::string & s,
                               const Fields & fields, ActivityId parent);
    virtual void stopActivity(ActivityId act);
    virtual void result(ActivityId act, ResultType type, const Fields & fields);

So it is a TREE of activities carrying typed progress results
(resProgress, resBuildLogLine, resSetExpected...), not a stream of
strings. `ActivityId` is a uint64 and `startActivity` names a parent.
A client that only receives log lines throws most of it away - the
progress bar every Nix user sees is built from activities and results.

## Why it does not fit the current shape

Everything today is unary-unary: a client asks, the server answers.
This is the first thing that travels the other way, unsolicited, so it
needs a server-streaming rpc. grpclib supports that; nothing in this
repo has used it.

Four things to get right.

**The trampoline is the easy part.** PyStore already forwards a C++
pure virtual to Python, so a PyLogger doing the same for log/logEI/
startActivity/stopActivity/result is a shape this repo has.

**That last sentence is no longer true.** `tasks/060` deleted every
trampoline along with the mock, and `nbemit.py` says so where one used
to be built: "There are no trampolines any more". So the shape this
paragraph called the easy part is not in the repo, and 033 - which
rested on the same sentence - is where its replacement gets built
first. A logger is an abstract CLASS, so unlike a primop it does need
a subclass; what 033 settles for it is the GIL half, which is the part
no code here demonstrates since the mock died.

**The thread is not.** Logs are emitted on whichever thread is doing
the work - an affine EvalState thread, or a pool thread - and the
callback re-enters Python there. It must not block that thread on a
socket, and it must not touch the event loop directly. A bounded queue
per connection, drained by the streaming rpc on the loop, is the
shape; the interesting question is what happens when it fills.
Dropping log lines is usually right and dropping a stopActivity is
not, because the client's activity tree would leak a node.

**Scoping.** `nix::logger` is a GLOBAL. A log emitted while serving
one connection belongs to that connection, but a shared LocalStore
touched by two connections has no single owner. Either the logger is
per-EvalState (which fits the affine model and covers evaluation, the
case that matters) or the server tags each record with the connection
whose call was in flight - which needs a context variable that
survives the hop onto a runner thread.

**Verbosity is a subscription, not a constant.** A client should say
what it wants when it opens the stream, and the server should be able
to skip work for records nobody asked for.

## Fit with the rest

`Fields` is a list of ints and strings, so a record is representable
without 030. An `ErrorInfo` is not - it carries a trace of positions -
and it overlaps with the typed-error path this repo already has
(WrapperError.to_dict crossing as JSON in the gRPC status). Those two
should agree rather than each inventing an error shape.

This is the first thing the surface cannot generate. Every rpc so far
came out of the manifest because it came out of a binding declaration;
a log stream is protocol, like Session. It belongs beside Session, and
the generator should stay unaware of it.

## Measured, 2026-09-03

Against the same source tree, and each line names where.

**`nix::verbosity` filters before any logger runs.** `printMsg` and
`logErrorInfo` both test `level <= nix::verbosity` and only then call
the logger (logging.hh:314, :330). So a subscriber cannot ask for more
than the global, and raising the global floods every other logger too.

**Activities are not filtered.** `Activity::Activity` calls
`startActivity` with no test at all (logging.cc:196). The `lvl` it
passes is a field of the record, not a gate. So the tree always
arrives in full and the level is a filter on MESSAGES only. Those two
sentences look like one sentence and are not.

**`curActivity` is `thread_local`** (logging.cc:23). A new activity
takes the current thread's activity as its parent, so the tree is
already per-thread upstream.

**This Nix evaluates on one thread.** Nothing under `src/libexpr`
names `eval-cores` or `evalCores`, so 2.34.8 has no parallel
evaluation. An `EvalState` therefore evaluates on the thread that owns
it, and thread identity is enough to say which state a record belongs
to. Records raised by a fetcher or a file-transfer thread carry no
such identity, and fall back to a process-wide sink.

**`Logger::Field` is a hand-rolled variant**: an unnamed enum
`tInt`/`tString`, a `uint64_t i` and a `std::string s`, with upstream's
own FIXME saying to use `std::variant` (logging.hh:76). A record
mirrors that rather than inventing a third shape.

## Decided

**The tap is a `Logger` subclass, and NOT `makeJSONLogger`.** Nix
already ships a backchannel: `makeJSONLogger(Descriptor fd, ...)`
writes every record as JSON, and `applyJSONLogger` points it at a file
or a unix socket. Reading that from Python needs no subclass. It was
rejected for three reasons.

It answers neither scoping question above: one process-wide stream can
say which connection a record belongs to only if something else tags
it. It still needs C++ to install, so it does not even save the
approval. And `JSONLogger::write` DISABLES ITSELF on a write error and
warns once (logging.cc:262), which is this repo's named failure mode
sitting in upstream: after that line every later record is gone and
nothing downstream can tell that from silence.

The JSON record shape is still the model to copy. `action`, `id`,
`level`, `type`, `text`, `parent`, `fields` (logging.cc:272-333) is
what a Nix client already expects to read.

**The subscription is a CLASS, not a method on `EvalState`.** An
`EvalState` is affine: every method on it routes to that state's own
thread, one at a time. A `drain_logs()` there would queue BEHIND the
`eval_file` whose progress it wants to report, so the records would
arrive only after the evaluation they describe had finished. The
subscription is therefore its own object with its own lock, on the
pool policy, and `EvalState` only hands one out. That is also the
shape this file already asked for - "a bounded queue per connection".

**The vocabularies do not become StrEnums.** `decl/words.py` earns its
shape from one sentence: "a member IS the string libstore parses".
Nothing parses `Verbosity`, `ActivityType` or `ResultType` from a
string; they are int-valued C++ enums that travel as ints in Nix's own
JSON. Forcing them into `words.py` would make that file's rationale
false. They cross as ints, and an int vocabulary is a DSL question for
later.

**The streaming rpc is deferred to its own commit.** The in-process
subscription is the mechanism; the rpc is one reader over it. Carl's
priority is that the binding and the wrapping agree, and that is the
in-process half.

## Built, 2026-09-03

**MOSTLY DONE.** The in-process half works. The rpc is deferred, and
now REFUSED rather than half-published.

`EvalState.subscribe_logs(capacity, level)` hands back a `LogStream`,
which is a bounded queue with its own mutex. `drain()` answers the
records waiting, `dropped()` counts what the bound refused, `close()`
stops it filling. `unsubscribe_logs()` clears the thread's
subscription. A record is a `LogRecord`, and its `fields` are
`LogField`s - upstream's own hand-rolled variant, mirrored rather than
flattened.

The C++ is `huggorm::LogTap`, `LogQueue`, `LogRecord` and `LogField`
in `cpp/eval.hpp`, approved by Carl on 2026-09-03. Every accessor
Python sees comes from `@reads` in the declaration; the tap itself is a
subclass no declaration can express, and `tasks/084` holds that gap.

`cpp/eval.hpp` went from 331 to 489 code lines. The sketch estimated
130 and it cost 158.

### What building it found

**An rpc that answered a handle nobody could use.** `subscribe_logs`
was published in the schema, returning a `LogStream` handle - and
there was no `LogStreamService`, because `annotate` skips a class that
is not `wrapped`. The comment there said why, and its reason was
false:

    An unwrapped class has no remote surface: it crosses as a value,
    so a caller already holds the object and calls it directly.

That held for every unwrapped class until this one, because all of
them were wire VALUES. `LogStream` is unwrapped - pool-threaded, and
no method of it can block - and a PROXY. So "unwrapped" and "crosses
by copy" came apart, and the emitter published half a surface without
saying anything.

Fixed as a RULE, not a blocklist: `wire_blocker` now refuses a return
whose type is a proxy with no service, so the next such case reports
itself. `test_no_rpc_surface` in `tests/test_logs.py` holds it.

**An async class named with nothing behind it.** `_policy.ASYNC_CLASS`
carried `'LogStream': 'AsyncLogStream'`, because `cppgen/manifest`
stamps those names on every proxy and `emitter` read the key rather
than the `wrapped` flag beside it. `server.adopt` does `getattr` on
that name, so the first handle it leased would have raised
AttributeError. Same gate covers it.

**One of the pair still crosses, on purpose.** `unsubscribe_logs`
has an rpc and `subscribe_logs` does not, which looks like a leftover
and is a consequence of the rule. The rule refuses a method whose
ANSWER is a handle nothing can use; `unsubscribe_logs` answers
nothing, so it does not apply. What a remote caller can do with it is
stop a subscription somebody else made on that state's thread - the
same power every shared handle already grants, since a second
connection holding an `EvalState` can `forget_file` on it too. Gated
in `test_no_rpc_surface`, so the asymmetry reads as a decision.

### What is still open

**The streaming rpc**, which is the point of the task's title. The
in-process queue is the mechanism it needs; the rpc is one reader over
it, hand-written beside Session, and it does not come out of the
manifest.

**A process-wide subscriber.** The tap routes by THREAD, so a record
raised by a fetcher or a file-transfer thread reaches no queue.
Evaluation is covered - that is the case that matters and the one
`tasks/016` needs - and a build's own output is not.

**The ErrorInfo overlap.** A `logEI` record crosses RENDERED, which is
what `JSONLogger` does. The typed-error path (`tasks/036`) crosses an
error as its parts. Those two should agree rather than each inventing
a shape, and neither this task nor 036 has decided which wins.

**Two states on one thread share a subscription.** Sound under the
affine model, where a state has its own thread, and wrong for a caller
who opens two states on the main thread. Gated, so it is stated rather
than discovered.

### The gates, broken on purpose

A gate that has not been seen to FAIL is not known to hold. Two of
these are C++ facts, so each cost a full rebuild. A scratch script
carried the edits and is gone; both edits and both failures are
written out below, which is the part worth keeping.

The suite went from 297 to 312: fifteen gates, two of them live.

**A stop becomes droppable.** `droppable` changed from
`action == "msg" || action == "result"` to `action != "start"`, which
is sharper than making everything droppable: a start still fills the
queue, so the failure is exactly "a start with no stop" rather than
"no records at all".

    FAILED tests/test_logs.py::test_a_stop_is_never_dropped

ONE gate, and every message gate beside it still passed. So the
asymmetry in the bound is tested rather than asserted.

**The level filter goes.** `if (r.action == "msg" && r.level > level_)`
deleted from `push`.

    FAILED tests/test_logs.py::test_the_level_refuses_what_it_did_not_ask_for
    AssertionError: assert 'refused' not in 'trace: kept... refused'
    1 failed, 300 passed, 10 deselected

One gate again. The warning arrives at level 1 through a subscription
that asked for 0, and nothing else in the suite notices.

**`test_no_rpc_surface` was not run failing**, and the same evidence
exists without another rebuild: the policy file BEFORE the rule
carried `_EvalState_subscribe_logs` in `METHODS` and
`'LogStream': 'AsyncLogStream'` in `ASYNC_CLASS`, and the one after
carries neither. That is the before-and-after the gate asserts,
measured rather than reasoned about.
