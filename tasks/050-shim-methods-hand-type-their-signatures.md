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
