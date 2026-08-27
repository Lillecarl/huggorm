# One table for the markers

**OPEN.** Where each declaration marker is legal, how many times, and
what it conflicts with - as DATA, driving validation and emission
instead of being restated in prose and in scattered `if`s.

## Where it comes from

Carl found `akoen/nanobindgen` (alpha) and asked whether it had
anything for us. It runs the OPPOSITE direction - tree-sitter over C++
headers, reading `@nb_*` Doxygen tags the author added, emitting
nanobind - so it cannot bind Nix at all: we do not own
`nix/store/store-api.hh` and cannot annotate it. Nothing to adopt
wholesale.

One idea is worth taking, and it is `tags.py`:

    TAG_SCHEMA: dict[str, Tag] = {
        "nb_static": Tag(target=_t("method"), arity="flag",
                         takes_value=False,
                         excludes=_e("nb_init", "nb_new")),
        "nb_prop_ro": Tag(target=_t("method"), arity="once",
                          excludes=_e("nb_init", "nb_new",
                                      "nb_prop_rw", "nb_name")),
    }

One table, driving BOTH validation and emission. Its `validate.py` is
then a generic loop over the table rather than one hand-written check
per rule.

## What we have instead

Nineteen decorators in `declare.py`, each a function that sets a field
on a `Decl` or writes an attribute on a function. Where each is legal
is stated in PROSE, in that decorator's docstring, and enforced
nowhere - or enforced by a bespoke `if` somewhere in `read.py`, which
carries 28 `DeclarationError` raises.

    derives abstract tree startup translator header produced words
    binding wire_value custom cxx_name blocks local reads needs
    instant threading binds

Rules that exist and are not data:

- `@reads` with parameters - refused by one hand-written check,
  written only when a declaration first hit it.
- `@needs` on a CLASS - worked by accident, then on purpose, and
  nothing says which targets it takes.
- `@instant` on a class whose `blocking=False` - meaningless, and
  unchecked.
- `@pure` excluded nothing while it existed; `@virtual` had no target
  restriction. Both are gone now (060), and neither absence was
  noticed by anything.
- `@local` on a method of a class that is not a wire value - harmless
  and silent.

This is the same class of bug the union arms hit, where "what may be
an arm" needed four separate refusals written by hand.

## What it would look like

A `MARKERS` table in `declare.py`, keyed by decorator name:

    target      which of class / method / free / module
    arity       flag, once, repeatable
    excludes    markers it cannot appear with
    requires    markers it needs (`@pure` needed `@virtual`)

`read.py`'s `_apply` already runs every decorator against a stand-in
and reads what was written, so it is one loop away from checking the
table at the same time. The 28 raises do not all go - some are about
SHAPE rather than markers, like "a declaration body is a docstring
then at most one Cxx" - but the marker ones collapse into the loop.

## Two smaller things from the same file

**Diagnostics carry `path:line:col`.** Ours carry a line number and
the text. A path matters once a declaration imports another and the
error is in the imported one.

**Errors are COLLECTED, not raised.** `ErrorCollector` gathers every
diagnostic and reports them together; ours raises on the first. Fine
for a build gate, worse for a person fixing three mistakes in one
declaration.

Their unknown-tag error also runs `difflib` for a "did you mean
@nb_name?" suggestion. Cheap, and it is the kind of thing that pays
for itself the first time somebody writes `@read` for `@reads`.

## When

AFTER the store hierarchy (060 step 3). That step adds markers -
`@derives` and `@abstract` on a real C++ base, where today they exist
only for the mock - and a schema written against what the markers
actually need beats one written against a guess. Doing it first would
mean designing the table for markers that are about to change.
