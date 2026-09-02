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
