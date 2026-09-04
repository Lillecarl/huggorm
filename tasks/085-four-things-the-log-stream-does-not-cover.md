# Four things the log stream does not cover

**OPEN.** Blocks nothing. `tasks/032` is done - a tap, a bounded
queue, and `Session/Logs` streaming it to another process. These four
are what that shape leaves out. Each is stated rather than fixed,
because each was a decision and not an oversight.

They are separate problems in one file because they share one cause:
the tap routes by THREAD, and a thread is not always the right owner.

## 1. A record raised on no evaluation thread reaches nobody

The tap keeps a `thread_local` queue, so `push` finds a subscriber
only when the raising thread has one. An evaluation is covered - an
`EvalState` is affine and this Nix evaluates on one thread - and
everything else is not: a fetcher thread, a file-transfer thread, and
a build's own output.

A build's log is what a Nix user most wants to see, so this is the
gap with the most weight.

The fix is a process-wide sink under the per-thread one: `push` falls
back to it when the thread has no queue. What that costs is the
scoping question `032` opened and did not answer - a process-wide sink
has no owner, so two connections reading it read each other's
records. Nix's own `nix::logger` is a global and has the same problem;
`makeJSONLogger` does not solve it either.

## 2. Two states on one thread share a subscription

Sound under the affine model, where a state owns a thread. Wrong for a
caller who opens two `EvalState`s on the main thread and expects two
queues.

Gated in `tests/test_logs.py`, so it is stated rather than
discovered. The fix needs the tap to route by something finer than a
thread, and nothing in a `nix::Logger` callback says which state
raised the record.

## 3. One reader per state, and no fan-out

`Session/Logs` refuses a second stream on a state that already has
one, with FAILED_PRECONDITION. It has to: a second `subscribe_logs`
REPLACES the first in the C++, so accepting would leave the older
stream open, connected and empty - indistinguishable from a state that
stopped logging.

Fan-out is the other answer: one subscription on the state's thread,
many readers over it. It is more code, and nothing needs it yet - a
second reader on one evaluation is a plausible want and not an
observed one.

## 4. An ErrorInfo crosses rendered, and 036 crosses one as parts

`logEI` renders the error to a string the way `JSONLogger` does
(`logging.cc:283`), so a log record carries TEXT. The typed-error path
(`tasks/036`) crosses an error as its declared parts, so the far side
rebuilds the class.

Those are two shapes for one thing, and neither task has decided which
wins. A caller that sees the same Nix error twice - once as a failed
call and once as a log record - sees two different objects.

Deciding it is cheap and doing it is not: an `ErrorInfo` carries a
trace of positions, which is a shape `036` has not had to represent
yet.

## 1 is DONE, 2026-09-04

Carl approved the C++ for it by name. `eval.hpp` goes from 1185 to
1289 lines, about 42 of them code and the rest the reasons below.

    huggorm.subscribe_process_logs(capacity=1024, level=3) -> LogStream
    huggorm.unsubscribe_process_logs()

### A fallback, not a broadcast

`LogTap::route` pushes to the thread's queue when there is one, and
to the process sink only when there is not. So a thread that
subscribed CLAIMS its records, and a state's subscriber sees exactly
what it saw before this existed.

That means the process sink does NOT answer "everything in this
process". It answers what nobody claimed.

Broadcasting to both was the alternative, and it buys the other
question at a price: a caller holding both subscriptions sees every
evaluation record twice, and a `LogRecord` carries nothing to
deduplicate by - no sequence number, no origin. Fan-out over ONE
subscription is the shape that answers "everything", and that is gap
3, still open.

Two gates, one per direction, because either alone passes for the
wrong reason. A `route` that dropped the record entirely would pass
"the process sink did not see it", and a broadcast would pass "the
thread's queue got it". Removing the fallback fails two gates.

### A mutex the thread_local did not need

`thread_queue` is a `thread_local` so `route` reads it lock-free on
the evaluation path. That rationale does NOT transfer: the process
slot is read from every thread while another thread subscribes,
which is a data race on the `shared_ptr` itself - two words, updated
non-atomically.

So the slot is behind a mutex. The cost falls only on threads with
no queue of their own, and `LogQueue::push` takes a mutex one line
later anyway. `std::atomic<std::shared_ptr<T>>` would be lock-free
and is not used: it is free-standing-optional in libstdc++, and this
lock is off the evaluation path entirely.

The replaced queue is closed OUTSIDE the sink's mutex, because
`close` takes the queue's own mutex and two locks taken in one order
here and the other order elsewhere is how a deadlock is built.

### REPLACE at the C++ layer, REFUSE at the rpc

The scoping question `tasks/032` opened - a process-wide sink has no
owner, so two readers read each other's records - is answered at two
layers, differently, and on purpose.

`subscribe_process_logs` REPLACES, like `subscribe_logs`. Refusing
here would let an in-process caller that drops its `LogStream`
without unsubscribing wedge the sink for the life of the process,
with nothing left to take it back. Replacing self-heals.

The rpc refuses a second stream with FAILED_PRECONDITION, the way
`Session/Logs` already refuses a second reader of one state - and
there a stream ending is what releases it, so a refusal cannot
wedge.

### What the bound means here, and it is not what it means there

A per-state queue is bounded by ONE EVALUATION, which is why a
`"start"` and a `"stop"` are never dropped: keeping them all is
affordable. A process-wide queue in a service that runs for days is
bounded by the READER draining it. A reader that stops draining
grows this without limit, and the capacity does not save it -
because the records it refuses to drop are exactly the ones that
accumulate.

Stated in the declaration rather than fixed. Fixing it means either
dropping a `"stop"`, which gap 3 of `tasks/032` argues against, or
ending the subscription when a reader falls behind, which is a
policy nothing has asked for yet.

### The codegen needed no change, which is the part worth recording

`subscribe_process_logs` is a FREE function returning a proxy with
no service - the same shape as `EvalState.subscribe_logs`, in a
place the emitters had never seen it. Nothing was taught. The
manifest, the async wrapper, the stub and the schema all derived the
same answer, including the refusal:

    NO_RPC['subscribe_process_logs'] = "return type: LogStream is a
    proxy with no service: it crosses as a handle, and nothing is
    wrapped to answer a call on that handle. A remote caller would
    receive an id it cannot use."

...and `unsubscribe_process_logs` got an rpc, for the same reason
`unsubscribe_logs` has one: it answers nothing. Gated on the emitted
REASON rather than on the absence, so a function that lost its rpc
for some other reason would not pass.

A `self` was never load-bearing. `EvalState.subscribe_logs` already
carries `(void) self` - the state was only ever a way to name a
thread - so a process-wide subscription has nothing to hang on and
is a free function.

### The rpc, and where the sharing is refused

`Session/ProcessLogs`, beside `Session/Logs` and hand-written for the
same reason: a log stream is PROTOCOL, the wire form of no declared
method.

    client.process_logs(capacity=0, level=None)   yields (records, dropped)

`LogsReq` carries a handle and `ProcessLogsReq` carries nothing,
which is the only structural difference. A request that took a handle
and ignored it would invite a caller to believe the scope was that
state's.

ONE response message for both. A batch of records and a drop count is
the whole answer either way, and a second message with the same two
fields would be the same fact declared twice. `_options` states the
two option fields once, numbered from a parameter because only one of
the requests has a handle in front of them.

The refusal is here rather than in the C++, and the split is the
answer to the scoping question. A second stream gets
FAILED_PRECONDITION - there is ONE sink, so accepting would replace
the first subscription and leave the older stream connected and
silent. It cannot wedge the way a C++ refusal would, because the
handler's `finally` clears the flag when the stream ends.

A BOOL, not a map: the per-state readers are keyed by state, and
there is nothing to key one process-wide sink by. That also makes the
limitation blunt - one reader for the whole SERVER, not one per
connection. The first connection to ask gets every unclaimed record
in the process and the second is told no. Fan-out is what would
change it, for this stream and for the per-state one, and it is gap 3.

`_pump` and `_log_stream` are the drain loop, written once. The empty
first batch, the drop reporting and the typed-failure contract are
decisions, and a second copy of each is a second place for one to
drift. What the two rpcs do differently happens before the loop:
which queue, and what `alive` means - a swept-connection check for
the per-state stream, and nothing for this one, which holds no lease.

### Four gates, and one that caught the schema itself

`test_the_descriptor_says_it_streams` asserts the EXACT set of
server-streaming methods, and it failed with `{'Logs',
'ProcessLogs'}` the moment the schema gained one. That is an
exact-set assertion earning its keep rather than a test needing an
update.

`test_a_state_stream_takes_its_records_back` opens both streams at
once, because the fact IS the relationship. Asserting only that the
state stream got the record would pass under a broadcast; asserting
only that the process stream did not would pass if the record
vanished.

Perturbations. Removing the fallback fails three, one of them the
remote gate with a TimeoutError. Removing the refusal fails one with
DID NOT RAISE. Removing the flag's clear from the `finally` fails
THREE - the one written for it, plus two later tests that inherit a
wedged server, which is what that leak would actually do.

One thing found by running it: `aclose()` on the client returns
before the server's `finally` has run, so the next subscribe races
it. The same retry `test_a_closed_stream_gives_the_state_back`
already needed, and it was written without one first and failed with
the FAILED_PRECONDITION the flag is supposed to have cleared.

### A race in both handlers, and why it has no gate

Found by review after the gates were green, and it was in
`Session/Logs` before `Session/ProcessLogs` copied it faithfully.

The claim came AFTER the subscribe:

    if self._process_reader: raise FAILED_PRECONDITION
    sub = await subscribe_process_logs(**opts)   # yields
    self._process_reader = True

`subscribe_process_logs` goes to the pool and `subscribe_logs` hops
onto the state's thread, so both yield. Two concurrent requests
therefore both passed the check, both subscribed, and the second
REPLACED the first's queue in the C++ - leaving the first stream
connected and silent, which is precisely what the refusal exists to
prevent. Whichever ended first then cleared the claim while the other
was still pumping.

The claim is taken before the first await now, with the `try` widened
so a subscribe that raises still releases it. `sub` starts as None,
and the `finally` closes nothing when the subscribe never returned
one - a raise from `subscribe_process_logs` installs nothing, because
the capacity and level checks run before the queue is made.

**NOT GATED, deliberately.** The failure needs two opens landing
inside one thread hop, so a test for it would assert a SCHEDULE
rather than a fact - and one that passed would say nothing about
whether the window was closed. This is `tasks/034`'s case: stated
where it can be read, in both handlers and here, rather than run.

### One power the sharing model already grants

`unsubscribe_process_logs` crosses the wire and its counterpart does
not, for the reason the pair on `EvalState` splits the same way: it
answers nothing, so the refusal that stops a `LogStream` handle
crossing does not apply.

So a remote caller can close the queue an open `Session/ProcessLogs`
stream is pumping, leaving it connected and silent with the claim
still held. That is the same power `EvalState.unsubscribe_logs`
already grants over a state's thread, and the declaration says so -
the per-state one carried that defence and this one did not, which
made it read as an oversight rather than as the model.
