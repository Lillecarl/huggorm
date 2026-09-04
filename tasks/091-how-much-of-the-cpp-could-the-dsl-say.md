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
