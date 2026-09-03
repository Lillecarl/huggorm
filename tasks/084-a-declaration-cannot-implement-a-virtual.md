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
