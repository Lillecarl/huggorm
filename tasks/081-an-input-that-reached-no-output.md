# An input that reached no output

**OPEN.** This repo's emitters SKIP what they do not recognise, and a
skip is indistinguishable from an absence. Three tasks found three
instances, each by someone writing the case rather than by a check.

## The three

- `tasks/073`: a version-branched class. `read.py` imports a
  declaration so Python resolves `if NIX_VERSION >= ...`, and the
  errors emitter parsed the file a second time and saw neither arm.
- `tasks/075`: an accessor written as a `@property`. `_live` names a
  definition by its `__code__.co_firstlineno` and a `property` object
  has none, so the accessor was dropped from the binding, from
  `_parts`, from `__repr__`, from `__hash__` and from `_wire_fields`.
  In silence.
- `tasks/078`: a whole declaration file in none of the lists. It
  imported, parsed, and emitted nothing, and the build succeeded.

Each emitted LESS than it should have and said nothing about it.

## What the gate would say

For one build: every declared class, method, field and word reached
at least one output. Not "the outputs are right" - that is what every
other gate is for - but "this input was read by somebody".

`tasks/078` closed the third case with a census of one directory
against four lists. That is the same idea at the coarsest possible
grain, and its shape is worth copying: count both directions, and
name each disagreement separately.

## Why it is hard

An emitter is allowed to skip. `@local` means an accessor never
leaves this side; a private method is bound and described nowhere; a
vocabulary with no user is still a vocabulary. So the gate cannot say
"unread is wrong" - it has to say "unread and not DECLARED unread is
wrong", and that means every legitimate skip has to become a thing a
declaration says rather than a thing an emitter does quietly.

That is the real work, and it is the same work as goal 2: a skip that
nobody declared is a decision an emitter is making on its own.

## Where to start

The reader already knows every class, method and field it read.
Instrument one emitter to record what it consumed, diff the two, and
read the list of the unconsumed. Whatever is on that list and is
legitimate is the vocabulary this gate needs.

## The first measurement, taken 2026-09-02

A crude probe: every declared class, method, word and free function
by NAME, searched for in the text of the emitted C++ and the emitted
Python package. 182 names.

Six did not appear in both. Every one of them is legitimate, and -
this is the finding - each is legitimate for a reason the declaration
ALREADY STATES:

| name | says what | consumed as |
| --- | --- | --- |
| `_init_libstore` (x2) | `@startup` | `huggorm::init_libstore();` at module init |
| `_gc_init` | `@startup` | `nix::initGC();` |
| `_translate_nix_error` (x2) | leading underscore | a registered exception translator |
| `open_store` | `@produced(by=...)` on Store | `Store`'s `nb::new_` lambda, and `_ctor_from` |
| every enum MEMBER | a vocabulary | `huggorm_bindings/words.py`, which is the C++ side's output |

So the fear this task was opened with - that legitimate skips are
undeclared, and each would have to be invented - is not what the
corpus shows. The vocabulary exists. `@startup`, `_`-private,
`@produced(by=...)` and "is a vocabulary" cover all of it, and each
is a fact a person wrote down for its own reasons.

That makes the gate buildable now, at the name grain, with a table of
four exemptions and no new declaration syntax.

## The gate, at the first of the two seams

**DONE for the reader seam.** `census_read` in `cppgen/generate.py`,
raising, run on every build after the two censuses beside it.

The three instances sit at TWO seams, not one. 073 and 075 are the
reader losing a node between the parse and its output; 078 is a file
the lists never named. 078's census covers the second at file grain.
This covers the first at method grain.

### Why it reads the RAW parse

That is the whole design. Everything downstream - the resolved tree,
the `Class` objects, the manifest, the emitted C++ - is built from
what the reader kept. A reader that drops a node wrongly makes every
one of them agree that it was never written. Comparing any two of
them proves nothing.

So the declared side is `ast.parse` of the file, and the consumed side
is where the reader PUT each definition: in `methods`, or as the
`ctor`, or as `from_parts`. Asking where derives the two exemptions
- `__init__` becomes the constructor, `_from_parts` becomes the
round-trip helper - instead of listing their names.

### The one exemption that is not a place

A `NIX_VERSION` arm that lost. Python resolves the branch during the
import, so one arm survives and the other is MEANT to vanish; the raw
parse still has both. Structural, not a name: a node inside an
`ast.If` is exempt.

No declaration in this repo branches, so that branch is read only by
a test declaration - which is exactly the condition that let 073
happen.

### Measured by breaking it

`tasks/075` reproduced against the real corpus: the descriptor lookup
removed from `_live`, and `PathInfo.registration_time` marked
`@property` - the accessor 075 actually measured.

    TypeError: a declaration writes definitions nothing reads.
    pathinfo.py: PathInfo.registration_time() is declared and reaches
    nothing - the reader kept no method, no constructor and no
    _from_parts by that name

Before this, that same state produced `pathinfo.cpp:99:
'registration_time' was not declared in this scope` - from the
hand-written `_from_parts` that still named it. A class whose
`_from_parts` is derived produced nothing at all.

A second attempt is worth recording because it failed for a better
reason. Marking `GCResults.bytes_freed` did not reach the census: it
carries `@reads`, and a marker over a `@property` raises during the
import, which `tasks/082` now refuses by name. Two gates, and the
earlier one fired.

### errors.py, and what it turned out not to be exposed to

`errors.py` is not read as a `Module` - it emits a Python module and
a C++ catch chain, both written from the tree - so the census asks it
the same question against the RESOLVED tree. Raw against resolved,
not against the emitted module: `resolved` is parse, then `_live`,
then `_reconcile`, then `_resolve`, so the comparison spans the seam
rather than comparing two things built from one read.

Measured, and it corrected a guess. A method there cannot be dropped
by this mechanism at all: `_resolve` appends a `ClassDef` WHOLE and
never filters its body, and the per-method filtering happens in
`_class`, which errors.py never reaches. So the `@property`
perturbation that fires on PathInfo fires nothing here.

The CLASS grain is the exposure. `_live` made to forget one class:

    errors.py: SysError is declared and the resolved tree has no such
    definition

That is an exception class gone from the emitted module and from the
catch chain, silently - which is `tasks/073`'s shape reached by a
different route than a version branch.

### What is still open

The EMITTER seam. This proves the reader kept a definition; it does
not prove any emitter wrote it. `emit_module` prints `not bound: ...`
at class grain and nothing at method grain, so an emitter that
skipped one method of a bound class would still say nothing.

That is the harder half, and it needs what this one did not: a record
of what each emitter consumed, rather than a comparison of two things
built from the same read.

## What the probe is NOT good enough for

It searches TEXT, and that is wrong in both directions.

It passes a name that only appears as a STRING. `open_store` "reached"
store.cpp as `cls.attr("_ctor_from") = "open_store"` before anyone
looked at what that meant.

And it passes on substrings. `BUILT` is inside `built_outputs`, so
several enum members counted as reached by an unrelated word. The
182-name pass rate is therefore an overstatement, and the six failures
are the only trustworthy half of the answer.

A real gate asks the emitters what they consumed, rather than
searching what they wrote. That is the design decision this task
still holds.

Opened 2026-09-02, while closing `tasks/078`. First measurement the
same day.
