# Are all the lines in the headers justified

**MOSTLY DONE.** Carl asked. The answer was no - ten lines, all of
them left behind by the split in `tasks/089` an hour earlier. They
are gone. Two questions the audit surfaced are open, and they are
why this is not DONE.

## The ten

Every one was justified in `eval.hpp` before the log tap and the
collector moved out of it, and nothing moved the includes with the
code.

    eval.hpp   <atomic>                  live_roots left
               <deque>                   LogQueue left
               <mutex>                   the queues and the sink left
               <sstream>                 logEI's rendering left
               nix/expr/eval-gc.hh       initGC was never called here
               nix/util/error.hh         showErrorInfo left
               nix/util/logging.hh       the whole tap left

    gc.hpp     nix/expr/eval-gc.hh       named in a comment only
               nix/expr/value.hh         named in a comment only
               class Bridge;             dead here

`class Bridge;` is the one that is not merely unused. `Evaluator::wrap`
answers a `Bridge` and is declared before `Bridge` is defined, so the
name has to exist first - it was load-bearing in `eval.hpp` and the
split carried it into `gc.hpp`, where nothing looks at it. It works
only because `eval.hpp` includes `gc.hpp`. Moved back.

Found by asking each file which symbols it names OUTSIDE a comment.
That last part matters: `gc.hpp` mentions `nix::allocRootValue` and
`nix::initGC` in prose, and grepping without excluding comments says
both headers are used.

## `<stdexcept>` stays, and the first reason given for it was false

`eval.hpp` uses nothing from it. The EMITTED file does:
`eval.cpp` includes this header and no standard one of its own, and
the `Cxx` bodies in the declaration throw `std::invalid_argument` 54
times.

The comment first written here said an include-what-you-use pass
would break the build. **It does not.** Removing `<stdexcept>` still
compiles, because `logging.hpp` reaches `nix/util/error.hh`, which
supplies the name. Measured after writing the opposite, which is the
fourth time in this session.

It stays anyway, and the corrected comment says why: that chain is an
ACCIDENT. Nothing about `eval.hpp` promises to include `logging.hpp`
forever, and the declaration's bodies break the day it stops.

`<cstdint>` is not in this class - `eval.hpp` uses `std::uintptr_t`
directly, for the identity a Bridge answers.

## OPEN: the emitter should own this

An include a header does not use, kept so that a GENERATED file
compiles, is a fact stated in the wrong place. The emitter knows what
a `Cxx` body spells: it could emit `<stdexcept>` beside the body that
throws, and this line could go.

Not done here because it is a real emitter change with its own gate,
and the audit was meant to answer a question rather than start one.

## OPEN: two things in `logging.hpp` that may be derivable

Neither is a defect and both are worth naming, because "justified"
was the question.

**`LogTap`'s five overrides.** This is `tasks/084` exactly - a
declaration cannot implement a virtual, so five methods say "this nix
callback means this record shape" in C++. It is the one MAPPING in
the headers by this repo's own definition. 084 is open, and it is
parked for the right reason: there is one implementer, so the shape
cannot be shown to generalise.

**`LogField` and `LogRecord`.** Plain structs with no methods, whose
fields the declaration reads one by one with `@reads`. The emitter
already knows every field and its type, from the declaration that
reads them - so in principle it could write the struct and the tap
could fill it. Whether that is worth a second emitter is not obvious;
it is recorded so the answer is a decision rather than an oversight.

## What was checked and found fine

`libstore.hpp` (48 lines, 12 of code) is one idempotent
`initLibStore`, and the file already records that its other half was
deleted as a mapping (`tasks/063`).

`errors.hpp` builds a Python exception from a C++ one. It names no
class and no field - the module, the name and the extra parts are all
PARAMETERS the emitter passes - which is what keeps it a helper.

`gc.hpp`'s two-flag registration is the one place a measurement is
the justification: one flag never latched on the main thread, and 500
`make_int` calls took 1124 ms instead of 0.17 ms.

`eval.hpp`'s `Reach` template is C++ standardese for getting at a
private member without patching nixpkgs, and it fails loudly if
upstream renames one.

Opened and mostly closed 2026-09-04, from Carl's question.
