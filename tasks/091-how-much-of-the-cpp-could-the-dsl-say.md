# How much of the C++ could the DSL say

**OPEN.** Carl:

> The emitter should indeed be able to emit includes, I'm surprised
> there's so much C++. Generally Python is very expressive with
> decorators and allowing "anything anywhere" (if statements in
> classes, bare module statements etc....) so much stuff should be
> expressable in our "Python DSL" to emit C++

This is the audit that question deserves: every construct in the
headers, its size, and whether a declaration could say it instead.

The answer is about a third, not most - and the reason for the
surprise is partly a number nobody is printing.

## The number IS printed, and this file first said it was not

**Corrected.** The first version of this section claimed
`CLAUDE.md:95` was stale - that nothing counts the directory. It is
accurate. `generate.census_cpp` counts it on every build:

    front door -> .../__init__.py: 34 name(s)
    hand-written C++ in cpp/: 593 lines in 5 file(s)
      errors.hpp: 35 lines, claimed by no module
      libstore.hpp: 12 lines, claimed by no module
      logging.hpp: 208 lines, claimed by no module

Two greps missed it and neither was enough: `nix run --file . check`
does not carry the bindings-src build's stdout, and searching for
"line count" and "wc -l" does not match `_code_lines`. The right
check was `nix log` on the derivation, and it took three minutes.

That is the THIRD claim of this shape in one session - `<stdexcept>`
and `errors.hpp`'s three includes were the first two - and it is the
worst of them, because it was committed and it accused the project's
own instructions of being wrong.

The number it reports is 593, which is CODE lines. So the figure
Carl is reacting to is already the honest one, and the "the comment
ratio inflates it" answer this file first gave is also wrong.

`census_cpp` also names the ORPHANS - a file no module claims, so no
declaration is emitted beside it. Three of the five are orphans, and
that is the more useful half of the report: `logging.hpp` alone is
208 lines that nothing derives.

## What is actually there

593 code lines, in 1545 total.

    errors.hpp      125 total    35 code
    eval.hpp        767 total   280 code
    gc.hpp          165 total    58 code
    libstore.hpp     48 total    12 code
    logging.hpp     440 total   208 code

## Derivable: about 200 lines

Each of these is SHAPE - a pattern the declaration already carries
enough information to generate.

    17  the `Reach` dance          three private members, and the
                                   [temp.spec]/6 boilerplate around
                                   each is identical. One decorator -
                                   `@private_member("fileEvalCache")`
                                   - could write all of it.
    62  `LogTap`'s five overrides  `tasks/084`. Each is a virtual
                                   signature plus a record literal
                                   mapping parameter to field.
    41  the four subscribe/        one shape four times: swap a slot,
        unsubscribe functions      close what was there, answer the
                                   new one.
    16  `LogField`, `LogRecord`    plain structs whose every field
                                   the declaration already names with
                                   `@reads`.
    16  `thread_queue`,            a typed slot, three times.
        `process_sink`,
        `process_queue`
    15  the three flag/counter     "a thread_local bool" and "a
        accessors                  process-wide atomic" are shapes.
     8  `init_libstore`            "call this once, ever" is a
                                   decorator, not a function.
     8  `Evaluator::wrap` and      two-line constructors.
        `wrap_builder`
     7  `make_core`                a shared_ptr with a deleter.
     6  `install_log_tap`          one policy statement.

## Not derivable: about 300 lines

Each of these is an ALGORITHM or a lifetime rule, and writing a
declaration form for one of them would be inventing a language to
express one program.

    106  `Bridge`                  root lifetime, staged lists and
                                   attribute sets, materialise,
                                   symbol interning. This is the
                                   binding's core, not a mapping.
     52  `LogQueue`                a bounded queue whose drop policy
                                   depends on the ACTION - never a
                                   start or a stop - under a mutex.
     45  `register_primop`         a C++ callback that reacquires the
                                   GIL and re-enters Python.
     31  the GC thread dance       two flags, a thread-exit
                                   destructor, and a measurement
                                   (1124 ms to 0.17 ms) as its
                                   justification.
     22  `EvalCore`                member ORDER is the fact. The
                                   order could be declared; the four
                                   objects and their references could
                                   not.
     22  `as_error`, `raise_as`    builds a Python object from a C++
                                   exception, with a fallback for
                                   when that itself throws.
     15  `forget_file`             two caches, collect-then-erase
                                   because a visitor holds a lock.
      8  `cached_files`            a visitor over a concurrent map.
      6  `gc_collect`              "twice" is the whole content.

## The pattern behind the question, which is the real finding

Carl's point stands separately from the count. Every time the DSL
meets something Python can express, it REFUSES rather than learns:

    @property        refused        tasks/076
    @staticmethod    refused        tasks/082, 076
    @classmethod     refused        tasks/082, 076
    a dunder         refused        tasks/088 - and SILENTLY until
                                    this week
    async def        refused        tasks/088
    a virtual        cannot be said tasks/084

Six, and five of them are open. That is not six unrelated gaps; it is
one habit. The DSL reads a declaration with `ast.parse` AND an
import, so it already sees decorators, class bodies, `if` branches
and bare statements - Carl is right that the raw material is there.
What it does with anything unfamiliar is decline.

Refusing was the right first move each time: a silent skip is worse,
and `tasks/088` found one that had been silent since the reader was
written. But six refusals is the point at which the answer stops
being "refuse the next one too".

## The order to take them in

1. **Includes**, which Carl agreed to and `tasks/090` has two
   instances of. The emitter knows every error class an emitted file
   catches and every type a `Cxx` body spells.
2. **`@private_member`**, the cheapest real one: 17 lines, three
   users already, and the pattern is pure boilerplate around a name.
3. **`tasks/084`'s virtuals**, which is the biggest single win at 62
   lines and also the hardest - it needs a way to say that this
   parameter of the virtual becomes that field of the record.
4. The rest, as anything needs them.

The baseline needs no work: `census_cpp` already reports it, and
`logging.hpp` being the largest orphan at 208 lines is the report
pointing straight at the biggest target.

Nothing here says the ~300 should shrink. A helper that is an
algorithm is what goal 2 explicitly ALLOWS, and pretending otherwise
would trade a readable 50-line queue for a declaration form nobody
else uses.

Opened 2026-09-04, from Carl's question.

## `@private_member` is later, and the reason is a dependency

It looked like the cheapest item, and it is not, because of who uses
the thing it would generate.

Every consumer of the `Reach` dance is a HAND-WRITTEN helper in
`eval.hpp`:

    cached_files          (state.*get(FileEvalCache{}))->cvisit_all
    forget_file           both caches
    register_primop       (state.*get(AddPrimOp{}))(std::move(op))

Generated code never touches it. So an emitted `Reach` block has to
be visible to `eval.hpp`, and `huggorm-decl` does not depend on
`huggorm-gen` - `packages/huggorm-bindings/default.nix` names all
three, and the arrow runs bindings -> {gen, decl, dsl} and no other
way.

Two ways out, and both are bigger than the 17 lines they save:

1. **`eval.hpp` includes a generated header.** It compiles, because
   nothing outside the bindings build ever compiles `eval.hpp`. But a
   helper is meant to be readable and buildable on its own, and this
   makes one that is not - it would stop compiling standalone, which
   is a worse property than the boilerplate it removes.
2. **The consumers move into the emitted file**, as `Cxx` bodies on
   the declaration. Then everything is on the generated side and the
   header loses ~40 lines rather than 17. Defensible, and it is a
   real change to three helpers rather than a decorator.

So this one waits on (2) being worth doing for its own sake. Carl
named `@private_member` as an EXAMPLE of the appetite - "things like
`@private_member` and other C++ annotations" - rather than as the
task, and the appetite is better spent where the emitter already owns
the output.

## Which makes includes the real first step

`tasks/090` has two measured instances and Carl agreed to it by name.
It also has no dependency problem at all: the emitter already writes
the include block of every emitted `.cpp`, so this is a fact it
computes instead of one a header carries on the emitted file's
behalf.

**DONE, both halves, 2026-09-05.** `BODY_HEADERS` derives the
standard headers a `Cxx` body spells, and `decl/errors.py` says
`header = "nix/..."` beside each `cxx` so the emitter writes the
translator's includes too. `tasks/090` holds the detail, including
that neither perturbation broke the BUILD - nix's own headers reach
`<stdexcept>` along some chain - so the gate is a text gate on what
the emitter derived.

So item 1 of the order below is finished. Item 2 is blocked, item 3
is `tasks/084`, and `tasks/084` got harder rather than easier - see
its own 2026-09-06 section.

## The dependency is not `@private_member`'s alone. 2026-09-05

`@private_member` waits on who CONSUMES what an emitter would write.
That test was applied to one item and it should have been applied to
the list, because `logging.hpp` is the largest orphan and most of its
derivable lines fail the same test.

Measured, by grepping every consumer outside the header:

    thread_queue          logging.hpp only
    process_sink          logging.hpp only
    process_queue         logging.hpp only
    huggorm::LogRecord    eval.py, as a `@binding` target
    huggorm::LogField     eval.py, as a `@binding` target
    huggorm::subscribe_logs      eval.py, inside a `Cxx` body
    huggorm::unsubscribe_logs    eval.py, inside a `Cxx` body

So the block splits in two, and the earlier list did not:

- **Blocked, for `@private_member`'s exact reason: 32 lines.** The
  three slots and the two structs. Every consumer is `LogTap`, which
  is hand-written and in the same header. A `@binding` naming
  `huggorm::LogRecord` is not a consumer of a DEFINITION - it is a
  type the emitter binds - so generating the struct would still leave
  `LogTap` needing it from the generated side.
- **Not blocked: 41 lines.** The four subscribe/unsubscribe
  functions. Their only callers are `Cxx` bodies in `eval.py`, which
  are already on the generated side.

**Which changes what `tasks/084` is.** It was listed as "the biggest
single win at 62 lines and also the hardest". It is also the UNLOCK:
generate `LogTap` and the 32 blocked lines stop having a hand-written
consumer. 62 plus 32 is most of what `logging.hpp` holds that is not
`LogQueue`'s algorithm.

The 41 unblocked lines are a separate question and this file does not
answer it. Moving a helper into a `Cxx` body is a RELOCATION, not a
derivation, and the census counts `cpp/` - so doing it for the number
would be spending a budget `CLAUDE.md` says is not one. It needs an
argument of its own.

**And `tasks/089`'s request id adds to this file**, which is worth
saying in the same place: about ten lines on the largest orphan, on
the block `tasks/084` would delete. Small, and pointed at the target.

## Remeasured 2026-09-06, after `tasks/089`, `095` and `096`

The audit above is from 2026-09-04 and its numbers have moved a lot.
`logging.hpp` was the largest orphan at 208 code lines. It is 356
now, and `cpp/` as a whole went from 593 to **793**.

    errors.hpp      110 total    32 code   (was 35)
    eval.hpp        936 total   335 code   (was 280)
    gc.hpp          165 total    58 code   (was 58)
    libstore.hpp     48 total    12 code   (was 12)
    logging.hpp     978 total   356 code   (was 208)
                              -----
                                793        (was 593)

`logging.hpp` is now 45% of every hand-written line in `cpp/`, and
that is the number Carl's question was about. It went UP.

### Where the 356 are

Per top-level definition, counted the way `census_cpp` counts:

     75  LogTap                    the five overrides. `tasks/084`.
     48  LogQueue                  the bounded queue's drop policy
     44  VerbosityDemand           NEW
     34  ThreadLevel               NEW as a class; was ~4
     18  subscribe_process_logs
     13  route
     13  unsubscribe_process_logs
     11  set_process_demand        NEW
     10  subscribe_logs
      8  unsubscribe_logs
      8  effective_verbosity
      6  process_queue
      6  LogField
     11  LogRecord
     30  six typed slots           thread_queue, process_sink,
                                   verbosity_demand,
                                   default_verbosity, thread_level,
                                   thread_request - 5 lines each
      4  install_log_tap

### The growth is mostly NOT derivable, and that is the finding

Of the 148 new lines, about 100 fall on the not-derivable side of
this file's own test:

    49  VerbosityDemand + its accessor   a counted registry with a
                                         floor and a reconcile step.
                                         An ALGORITHM.
    30  ThreadLevel becoming a class     a lifetime rule: a
                                         destructor that gives a
                                         demand back, and a DELETED
                                         copy assignment that exists
                                         because the aggregate form
                                         was wrong (`tasks/096`).
    11  set_process_demand               the same pairing for the
                                         process, under a mutex,
                                         because the atomic form
                                         raced.
     4  route                            grew with the request id.

So `logging.hpp` did not grow because the codegen fell behind. It
grew because `tasks/089`, `095` and `096` were concurrency and
lifetime work, which goal 2 explicitly allows a helper to be. A
declaration form for "count holders per level and reconcile a global"
would be inventing a language to express one program.

The rest, about 48 lines, is derivable and joins the list above.

### Two of the earlier figures change

**`tasks/084` is worth more.** `LogTap` was 62 lines and is 75. With
the 32 lines it unblocks - the slots and the two structs, whose only
consumer is `LogTap` itself - it is now over 100.

**"A typed slot, three times" is six times.** `verbosity_demand`,
`default_verbosity` and `thread_request` joined `thread_queue`,
`process_sink` and `thread_level`. Six identical five-line accessors,
30 lines, and the shape has not changed at all: a function-local
static of some type, returned by reference. Two of the six leak
deliberately and say why; the decorator would have to carry that.

That is now the second-cheapest derivable item after includes, and
unlike `@private_member` it has NO dependency problem: every consumer
is in `logging.hpp`, which is also where the definitions are. The
emitter would have to write into that file, which is the same
blocker - so it waits on `tasks/084` too.

### What this says about the habit

Nothing new, and that is worth recording. `tasks/089` through `096`
added no refusal and no new marker, so the list of six declines is
still six. The DSL was not asked anything it could not answer,
because the work never reached it.
