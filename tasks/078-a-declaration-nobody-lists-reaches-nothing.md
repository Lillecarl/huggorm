# A declaration nobody lists reaches nothing

**OPEN.** `huggorm_decl/__init__.py` holds three lists - `NANOBIND`,
`VOCABULARIES` and `ERRORS` - and a declaration file that appears in
none of them is skipped. Silently.

## How it was found

`decl/gc.py` was written, was well-formed, imported, parsed, and
emitted nothing at all. `nix build bindings-src` succeeded and wrote
no `gc.cpp`. The file was simply not in `NANOBIND`, and nothing said
so (`tasks/074`).

The failure looks exactly like a declaration with no classes in it.

## Why the lists are right

They are not the problem and should stay. `__init__.py` says why:

> Which declarations exist, and which of them owns a compiled module,
> is a fact about this set of documents. An emitter is handed the set;
> it does not own it.

A glob would decide by filename what the author should decide, and
`decl/README.md` is already in that directory. The list is the
statement; what is missing is the check that the directory agrees
with it.

## What to do

One gate, in the reader or in `corpus()`: every `*.py` in `decl/`
appears in exactly one of the three lists, and every name in a list
exists on disk. Both directions, because they are different
mistakes - a file nobody listed, and a list naming a file somebody
deleted.

Prove it by breaking it, both ways. `decl/README.md` shows the
exclusion is not "everything in the directory": the gate reads `*.py`,
and a non-Python file in there is not a declaration.

## Why it is worth doing

This is the third silent drop found in three tasks. `tasks/073` was a
version-branched class the errors emitter never saw; `tasks/075` was a
`@property` accessor `_live` could not name; this is a whole file.
Each one emitted less than it should have and said nothing, and each
was found by someone writing the case rather than by a check.

The pattern is worth naming: this repo's emitters SKIP what they do
not recognise, and a skip is indistinguishable from an absence. A gate
that says "this input reached no output" would have caught all three.
That larger gate is the interesting version of this task; the list
check is the cheap one that closes the case at hand.

Opened 2026-09-02, while closing `tasks/074`.
