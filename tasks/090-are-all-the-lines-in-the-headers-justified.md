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

## A second pass, and a second wrong prediction

The first pass answered Carl's question about the two files the split
touched. He asked about ALL of them, so the other three got the same
treatment.

### `errors.hpp`: a stale sentence and three includes that are not its

Its opening comment ends "The catches are ordered most-derived first,
because a base class catch would swallow its subclasses." **There are
no ordered catches in the file.** There is one `catch (...)`, in the
fallback. The translator that has the ordered catches is EMITTED, one
`catch` per declared error class, and this file is the two helpers it
calls. The comment described a neighbour.

The file names exactly one nix symbol outside a comment:
`filterANSIEscapes`, from `terminal.hh`. The other three includes -
`store-api.hh`, `store-dir-config.hh`, `error.hh` - are for the
emitted files, which include this header and then catch
`nix::InvalidPath`, `nix::BadStorePathName` and their kind.

**Predicted that removing them would break the build. It did not.**

    391 passed

The emitted files reach those types through their own nix includes.
That is the SECOND wrong prediction of this shape in one session -
`<stdexcept>` in `eval.hpp` was the first, an hour earlier, and the
lesson did not transfer because the second case looked more obviously
load-bearing than the first.

They stay, for the reason that survives: `path.cpp` includes
`nix/store/path.hh` and catches `nix::InvalidPath`, which is a
store-api type, so it compiles through a transitive include nobody
declared. Keeping these puts the types at the one header every such
file does include - a weaker accident than the alternative, and
labelled as an accident rather than left to read as a need.

### Two instances make the emitter question concrete

The open item above was one line about `<stdexcept>`. It is now two
instances with the same shape, and the fix is the same for both: the
declaration names every error class the emitted file catches and every
type a `Cxx` body spells, so the EMITTER could write the include
beside the code that needs it. Both blocks would go.

That is worth its own task when somebody wants it. Named here rather
than opened, because nothing is broken - the build is correct today,
just correct by accident in three places instead of one.

### Whitespace

Four double blank lines, three of them left by the split and one
older, in `errors.hpp`. Removed. Trivial, and mentioned only because
the question was whether every line is justified and a blank line
that nothing put there deliberately is not.

## The emitter owns the includes now, half of it

`tasks/091`'s first step, and the half with two measured instances.

`nbemit.includes` derived the headers a translation unit needs from
its declared TYPES. It did not look at what the hand-written BODIES
spell, so the bodies' headers were carried by `eval.hpp` on the
emitted file's behalf - a fact about generated code living in a file
a person maintains.

`BODY_HEADERS` is that fact, derived. Fifteen spellings mapped to
their standard header, scanned across every `Cxx` body in the unit -
methods, the constructor, `_from_parts`, `@custom` blocks and free
functions. The emitted files change by exactly this:

    eval.cpp      + <algorithm> <cstddef> <cstdint> <stdexcept>
    gc.cpp        + <cstdint>
    hash.cpp      + <cstddef>
    pathinfo.cpp  + <cstdint> <utility>

...and `eval.hpp` loses the `<stdexcept>` it never used.

Only what a caster does NOT already bring. `<string>` and `<vector>`
arrive with `nanobind/stl/string.h` and its kind, so listing them
would add a line that is already there.

### The compiler cannot gate this, and the gate says so

Both perturbations were run and NEITHER broke the build. Removing
`eval.hpp`'s include compiles. Removing the derivation as well still
compiles - nix's own headers reach `<stdexcept>` somewhere along the
chain.

So the question the compiler answers is not the question. What
matters is whether the EMITTER derives the include, and a text gate
answers that: `test_a_body_brings_its_own_standard_header` asserts
`eval.cpp` gets `<stdexcept>` because its bodies throw, and that
`pathinfo.cpp` gets `<cstdint>` and `<utility>` and NOT `<stdexcept>`
because its bodies spell those and throw nothing. The control is what
says the derivation reads the body rather than adding a fixed list.

Removing the derivation fails it, and only it:

    1 failed, 381 passed, 10 deselected

### What is left of this item

The error translator's catches. `path.cpp` catches `nix::InvalidPath`
while including only `nix/store/path.hh`, and `errors.hpp` carries
`store-api.hh` for it. That half needs the error DECLARATIONS to say
which header defines their `cxx = "nix::..."` - eight classes in
`decl/errors.py` carry a C++ name and no header - which is a
declaration change rather than an emitter one, and it is the "bind
Python fake types to C++ types" shape Carl described.
