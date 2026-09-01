# A version-branched error class reaches nothing

**OPEN.** `pyerrors.entries` and `pyerrors.chain` read `tree.body`, so
they see TOP-LEVEL classes only. `pyerrors.module` copies the whole
document. A class under `if NIX_VERSION >= ...` therefore reaches the
emitted `errors.py` and reaches nothing else.

Measured on 2026-09-02, while closing `tasks/072`. The perturbation
was six lines appended to `decl/errors.py`:

    if NIX_VERSION >= (99, 0):
        class NeverImports(NixError):
            """A perturbation: the tree names it, the import does not."""

            cxx = "nix::NeverImports"

`nix run --file . check` said **all checks passed**, and the emitted
module carried this:

    if NIX_VERSION >= (99, 0):

        class NeverImports(NixError):
            """A perturbation: the tree names it, the import does not."""
            cxx = 'nix::NeverImports'

Three things are wrong there and each is its own hole.

1. **The class is in no manifest entry and in no catch clause.**
   `entries()` iterates `tree.body`, so the nested ClassDef is
   invisible. A caller can `except NeverImports` and nothing will
   ever raise it, and the wire has no `NeverImportsFault` to carry
   one.

2. **The `cxx` line is copied through.** `module()` strips a `cxx`
   assignment from a top-level class body and nothing walks into an
   `If`, so the emitted class keeps an attribute that names a C++
   type - in a module whose whole point is to be plain Python.

3. **The emitted module imported `huggorm_dsl`.** The perturbation
   needed `from huggorm_dsl.declare import NIX_VERSION` to parse, and
   `module()` copied that import into the runtime package. It IMPORTED
   at test time, which is the part worth noticing: the build-time DSL
   is reachable from the emitted bindings, so nothing failed.

## Why it is worth doing

A version branch is a shape the DSL invites. `declare.py` documents
it for a method, and `read.py` imports every declaration precisely so
Python resolves the branch. An error declaration is a declaration, so
the branch is legal there and means nothing.

The failure mode is the quiet one. Nothing crashes: a caller gets a
class that cannot be raised, and the day upstream splits an error
behind a version is the day someone writes this and believes it.

## What to do

Decide which of two answers is right, and both are defensible.

- **REFUSE the branch.** `entries()` raises if any ClassDef is nested,
  saying an error declaration is flat. Cheapest, and honest: no
  declaration needs the branch today.
- **READ the branch.** Walk into an `If` the way the import already
  does, and take the classes the IMPORT defines rather than the ones
  the tree names. `entries()` already has the module - it uses it for
  `__bases__` - so this is the reading it half does.

Whichever it is, PROVE it: the perturbation above must stop the build.

Found because `tasks/072` asked for a gate to be shown failing and
the closest thing to one was measured instead of assumed.
