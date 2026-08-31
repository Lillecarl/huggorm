# One table for the markers

**THE TABLE IS BUILT. THE `@abstract` SPLIT IS NOT.** Where each
declaration marker is legal, how many times, and what it conflicts
with - as DATA, driving validation and emission instead of being
restated in prose and in scattered `if`s.

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

**One marker means two things, and the case that separates them is
real.** `@abstract` says "the C++ type has pure virtuals" in the
declaration. Three emitters read it as "Python may not construct one":
`nbemit` skips the constructor, `emitter.py` refuses `Async<X>()` and
skips the wrapper, and `smoke_test` requires the refusal.

`nix::Store` is both abstract AND opened through `nix::openStore`, so
declaring the true fact about it breaks all three - verified, 24
tests. Every store this repo hands back is really a `nix::LocalStore`
or a `nix::UDSRemoteStore`, and the declaration cannot say so today.

The fix is the shape this task is about: the declaration states the
FACT, and the manifest carries the DERIVED question each layer asks -
"is there a door" - computed once instead of re-interpreted three
times.

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

**That precondition is dead, and waiting on it is waiting for
nothing.** 060 closed by DELETING the mock rather than porting the
hierarchy. `@derives` is gone from `declare.py` entirely, and
`@abstract` has no users at all. There is no step coming that adds
them, so the table was right to be built without it - and the
`@abstract` split is now blocked on a DECISION rather than on work.

## Done

The table exists. `declare.MARKERS` is 23 entries keyed by decorator
name, each carrying `target` / `arity` / `excludes` / `requires`, and
every marker in `declare.py` has one.

`read._check_markers` is the generic loop the task asked for, called
from `_apply`, so every marker on every target passes through it
once. It checks the legal target, flag-vs-called arity, "may appear
once", and both directions of `excludes` / `requires` - reporting a
conflicting pair once rather than from both ends.

The unknown-marker suggestion is in: `difflib.get_close_matches`, so
`@read` for `@reads` says which was meant.

The marker set grew while this was open. 19 became 23: `tagged`,
`produces`, `fills`, `names` and `guard` arrived, `derives`, `pure`
and `virtual` went with the mock. All 23 are in the table, which is
what the loop's unknown-marker branch enforces the moment one is
used.

`census_markers` now prints, beside `census_cpp`, which markers no
declaration carries. It names two: `@abstract` and `@custom`. That is
the measurement the rest of this task turns on - `@abstract`'s three
emitter branches and its smoke-test assertion have never run, and the
only place that said so was a comment inside the branch that never
fires.

## Left

**The `@abstract` split**, which is the design question and the
reason this file stays open. `@abstract` means "the C++ type has pure
virtuals" in the declaration and "Python may not construct one" in
three emitters. `nix::Store` is both abstract AND opened through
`nix::openStore`, so stating the true fact today deletes its factory:
`nbemit` takes the `if decl.abstract` branch INSTEAD of the
`elif decl.built_by` one, and `Store("dummy://")` stops existing.
Verified by reading the branch order, not by running it.

The fix is the shape above: the declaration states the FACT, and each
layer's DERIVED question - "is there a door" - is computed once. That
is one new field and a rename, not a redesign.

**`path:line:col` in diagnostics.** `DeclarationError` still carries
`line N: message` and no path. It matters once a declaration imports
another and the error is in the imported one - which is now normal,
because `decl/store.py` imports five others.

**Collected errors, not the first one.** Still raises on the first
diagnostic. Fine for a build gate, worse for a person fixing three
mistakes in one declaration.
