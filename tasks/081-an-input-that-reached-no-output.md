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

Opened 2026-09-02, while closing `tasks/078`.
