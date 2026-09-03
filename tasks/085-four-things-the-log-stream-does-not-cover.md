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
