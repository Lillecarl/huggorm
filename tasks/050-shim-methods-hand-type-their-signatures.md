# A shim-backed method states its types twice, with no cross-check

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

`c_store.pxd` declares 2 methods directly on CStore and 15 through
`_cpp/store.hpp` shims. The direct ones get their types backfilled
from the pxd (`_pxd_signature_table`), so the pyx states them once.
The shim-backed ones do not: the shim is a free function in the
pxd, the backfill only reads pxd CLASSES, and so every shim-backed
method hand-types its full Python signature in the pyx - which the
docstrings on `add_to_store` and `real_path` explain per method.

Two costs:

- The same signature exists twice (pxd shim declaration, pyx
  annotation) and nothing compares them. An annotation that drifts
  from its shim - `int` where the shim takes `string` - builds
  fine and fails at the first call.
- Adding a store call means writing its types three times: the hpp
  shim, the pxd declaration, the pyx annotation. The repo's rule is
  "avoid hand-typing what can be derived from the bindings", and
  this is the largest remaining hand-typed surface. At 15 methods
  it is a chore; at a real store surface it is the maintenance
  burden the codegen exists to remove.

Confirmed with cython-worker: not considered, a straight
oversight. The pyx even documents the workaround per method
("Annotated Python-style... there is no pxd declaration to
backfill the types from") - noticed as a nuisance, never as a
missing check. The existing gate catches only `Any`; a WRONG
hand-typed annotation passes the build.

## Fix sketch

A method-level declaration, then a cross-check, in steps:

1. A per-method link from a pyx method to the pxd free function
   that backs it. A name convention is NOT enough: the link is
   many-to-one and names differ (`path_info(CStore&, ...)` backs
   `Store.query_path_info`). So it needs a declaration - the
   method-level sibling of the class marker `_binds`, e.g. a class
   attribute `_method_binds = {"query_path_info": "path_info"}`,
   since cdef methods take no decorators. The generator drops the
   leading store parameter and backfills like
   `_pxd_signature_table` does. This is the "metadata next to the
   binding" rule applied to the one category it has missed.
2. Cross-check, not just backfill: where BOTH a live annotation and
   a pxd declaration exist, compare them and fail the build on
   drift. This also hardens the 2 direct methods.
3. Later, and only if the boilerplate keeps growing: emit the pyx
   marshalling itself from the pxd (the encode-args / nogil-call /
   decode-result body is mechanical). That is a design change, not
   a patch - the docstrings and the upstream-behaviour comments in
   the pyx are hand knowledge and must stay hand-written, so the
   generated part would have to live under the hand-written one.
   Do not start it before steps 1-2 prove insufficient.

## The end state (from the 2026-08-26 review discussion)

Step 3 is not just an optimization; it is the destination. The
durable design decision is declaration-primary, not
implementation-primary: the declaration file (pxd plus markers) is
the only hand-written artifact per API, and the binding
implementation is generated from it. The compiler then checks the
declaration against the real Nix headers on every build, which is
the property that makes the whole stack trustworthy.

That structure also keeps the binder replaceable. Most of
`_cpp/store.hpp` exists because a pxd cannot say `std::set`,
`std::optional` or a non-default-constructible return - things
nanobind's STL casters handle natively, with first-class
trampolines for the callback work in 032/033. If Cython's costs
keep growing (internal-API parser, `__cinit__` opacity, getset
descriptors), a generated binding layer makes a backend swap a
contained project: the manifest and everything above it would not
notice. No migration is proposed now; steps 1-2 are the work, and
they aim at this shape.

## Parked, 2026-08-26

Superseded before it started. Carl is considering a Python-IDL source
format: plain `.py` declaration files as the single source of truth,
from which the generator emits the pxd AND the pyx.

Steps 1-2 are scaffolding for a HAND-WRITTEN pyx. A generated pyx
cannot drift from its declaration, so a marker linking the two and a
check comparing them both describe a problem that would no longer
exist. Nothing here is started, and nothing should be.

The "end state" section above survives, and is the reason: it is what
the IDL idea arrives at, faster.

## What the spike found, 2026-08-26

`spike-idl/` holds the investigation. Two things came out of it, and
the second replaced the first.

**A Python IDL works.** `path.pyx`, `path.pxd` and `c_path.pxd` were
regenerated from a plain `.py` declaration read by import, compiled in
place of the hand-written ones, and passed the whole gate. Ignoring
comments and docstring wording the diff was empty. `spike-idl/README.md`
has it, including four bugs that emitting found and a template would
have shipped - three of which compiled fine.

**Cython's pure mode is the better target.** Carl asked whether it
could be extended rather than worked around. It can, in about ninety
lines using Cython's own idiom, and then the implementation file IS the
declaration: the same file compiles as a C++ extension type and imports
as ordinary Python. Task 054 has the evidence and the costs.

Under that shape a module is two hand-written `.py` files and two
GENERATED `.pxd` files - so the duplication this task exists to
complain about is gone, because both pxds are derived. Steps 1-2 above
stay superseded either way.

Three costs are on the record and one is sharper than it looks:
`new` is a SyntaxError to Python, so allocation goes through a smart
pointer AND through a factory declared in the pxd, because
`make_shared` carries libcpp's own `except +` and would swallow a
custom exception translator. Typed libstore errors are the whole
reason this repo binds C++, so that shim is not optional.

**The decision is open.** Nothing in the three packages has moved.
