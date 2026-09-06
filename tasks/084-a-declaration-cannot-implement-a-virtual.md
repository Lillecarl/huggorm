# A declaration cannot implement a virtual

**OPEN.** The log tap is a hand-written `nix::Logger` subclass. The
declaration can say "Python may not construct one of these" and cannot
say "implement these virtuals", so the five overrides were written by
hand.

## What was written

`huggorm::LogTap`, in `cpp/eval.hpp` (`tasks/032`, approved by Carl on
2026-09-03). Five overrides of `nix::Logger`, and each one is the same
sentence:

    void stopActivity(nix::ActivityId act) override
    {
        route({.action = "stop", .id = act});
    }

Take the parameters upstream passes. Put them in a record. Route the
record. Nothing else.

That is a shape, and a shape is what an emitter is for. Five copies of
it is exactly the case goal 2 names: "a rule applied identically in
twelve places belongs in the emitter, not in twelve places."

## Why it is not a mapping

It is not, and that matters for what this task is worth.

A MAPPING says "this Python name means that C++ call", and every one
of those is generated here already. `LogTap` faces the other way: it
is a callback RECEIVER, like `Bridge` and like the lambda
`register_primop` hands to `nix::PrimOp`. Generated code calls it -
`install_log_tap` is bound from the declaration - and no Python name
resolves to it.

So this is not a rule being broken. It is a rule the DSL cannot yet
express, and the cost is that a second abstract class to implement
would be a second hand-written subclass.

## What a declaration would have to say

Three things, and only the first is obvious.

**Which class, and which virtuals.** `@implements("nix::Logger")` and
one `def` per override, with the C++ signature the base declares. The
signature is the hard half: `startActivity` takes six parameters of
four different C++ types, two of which are enums this repo carries as
integers, and one of which is `const Fields &` - a vector of a
hand-rolled variant. A declaration that had to restate all of that
would be longer than the C++ it replaces.

**What the body does.** Here it is uniform, which is what makes it
generatable at all: build one record, route it. A second implementer
would almost certainly want something else, and a DSL that can express
an arbitrary body is a language rather than a declaration.

**Who owns the instance.** `install_log_tap` puts the tap under
`nix::logger` through `makeTeeLogger`, which takes a `unique_ptr` and
keeps it forever. A different base would be owned differently.

## Why it is not urgent

There is ONE implementer. A DSL feature with one case cannot be shown
to be right - the second case is what says whether the shape
generalises, and this repo has learnt that lesson twice already
(`tasks/074` on flattening a struct, `tasks/063` on a factory that had
to be a named symbol).

So the honest order is: leave `LogTap` hand-written, and open this the
day a second abstract class needs implementing. `nix::Store` does not
count - it is implemented by upstream, and this repo binds the base.

The line count is the thing to watch. `cpp/eval.hpp` grew from 331 to
489 code lines for the tap - 158, where the sketch Carl approved
estimated 130. The extra is the queue's accessors and the record
structs, not the overrides; the five overrides and their `convert` are
about 45 of it. The build prints the total on every run.

## Three facts in this file are stale, 2026-09-06

The conclusion is unchanged and the premises under it have moved.
Corrected rather than rewritten, because what moved is the argument's
own evidence.

**It is not in `cpp/eval.hpp`.** `LogTap` lives in
`cpp/logging.hpp`, split out when `eval.hpp` had grown to hold four
unrelated things. The line figures below refer to that file.

**`install_log_tap` does not use `makeTeeLogger`.** This file's third
requirement - "who owns the instance" - says the tap goes under
`nix::logger` through a tee that keeps it forever. `tasks/089` step 4
made it a REPLACEMENT:

    nix::logger = std::make_unique<LogTap>();

A tee kept the previous logger as the MAIN one, so every record
reached stderr whether a subscriber took it or not, which a client
reading the protocol over stdin/stdout cannot have. The ownership
question the requirement names is still real - a `unique_ptr` held
by a global - but the mechanism named is gone.

**There are SEVEN overrides, not five**, and this is the one that
matters. `writeToStdout` and `isVerbose` joined the original five.

## The uniformity premise is the one that broke

This file's second requirement said the body is generatable *because*
it is uniform: "build one record, route it". That was true of five
identical overrides. It is not true of the seven:

    log             gate on effective_verbosity, THEN route
    logEI           gate, RENDER the ErrorInfo to a string, route
    startActivity   route unconditionally; gate only the FALLBACK
    stopActivity    route. the original shape, and the only one left
    result          route
    writeToStdout   delegate to log, at lvlInfo
    isVerbose       return true

Four different shapes across seven methods. The asymmetry is not
incidental either - `startActivity` routes unconditionally because a
dropped start leaves a node in a reader's activity tree that nothing
ever closes, and `logEI` renders because an `ErrorInfo` carries a
trace of positions that `tasks/036` has not decided how to cross.

So a declaration form would now have to express a per-override GATE,
a per-override transformation, and two methods that build no record
at all. That is the "arbitrary body" this file already named as the
line between a declaration and a language - and the overrides walked
across it while nobody was generating them.

## What that does to the case for doing this

It weakens it, and `tasks/091` should read this before quoting the
line count again.

91 lists 084 as "the biggest single win" and as the UNLOCK for 32
further lines. Both are still true by the numbers - `LogTap` is 75
code lines now rather than 62 - but the numbers were never the
argument. The argument was that five copies of one sentence belong in
an emitter, and there are no longer five copies of one sentence.

The original reason to wait is unchanged and now has company:

1. There is still ONE implementer, and a DSL feature with one case
   cannot be shown to be right.
2. The one case is no longer uniform, so generating it would mean
   inventing per-override syntax for a gate and a transformation -
   for a single class.

`stopActivity` and `result` are the two that still read as the shape
this task was opened about. Two is not five.

## What did NOT change

It is still not a mapping. `LogTap` is a callback RECEIVER; generated
code calls `install_log_tap`, and no Python name resolves to an
override. Goal 2 allows it as a helper, and the census counts it.

And the unlock is real: the 32 lines `tasks/091` names - the typed
slots and the two record structs - have `LogTap` as their only
consumer, so they stay hand-written while it does.
