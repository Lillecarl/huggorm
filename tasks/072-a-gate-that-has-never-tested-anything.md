# A gate that has never tested anything

**OPEN.** `pyerrors.declared(mod)` answers "every exception class this
declaration names, for a gate", and it has been answering `[]` since
it was written.

    def declared(mod: Module) -> list[str]:
        """Every exception class this declaration names, for a gate."""
        return [cls.name for cls in mod.classes]

`mod.classes` is built by a loop that takes only DECORATED classes:

    if not (isinstance(n, ast.ClassDef) and n.decorator_list):
        continue

An error declaration wears no decorator. `cxx = "nix::Error"` is a
bare assignment, because there is no behaviour to mark and the
assignment reads as the fact it is. So every class in `decl/errors.py`
is skipped, `mod.classes` is empty, and whatever gate reads this list
has been checking nothing against nothing.

Found on 2026-09-01 while making a declared error a FIELD type
(`tasks/071`). That work added `Module.errors`, read from the BASE
rather than from a decorator, so the names ARE available now - this
function just does not use them.

## What to do

Two halves, and the second is the point.

1. `declared()` reads `mod.errors` instead of `mod.classes`. One line.

2. **Find what reads it and prove the gate fails.** A list that is
   always empty makes every caller vacuously true, so it is not
   enough to fix the source: the thing it feeds has to be shown to
   refuse something. Delete a class from the declaration, or add one
   the emitted module does not define, and watch the build stop. If
   nothing stops, the gate is not a gate and this task is about
   deleting it rather than fixing it.

## Why it is worth doing

Goal 1 says a gate that has never failed has not been shown to test
anything. This one is stronger than that: it is a gate that CANNOT
fail, and it sat in the tree looking like coverage.

It is also cheap. The names are already read.
