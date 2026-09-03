# Logs flow back to the client

**OPEN.** No log stream exists. `nix::Logger` is not bound and nothing
carries a line back to a client.

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
