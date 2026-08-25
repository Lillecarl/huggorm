# Real-Nix binding spike

Swap the mock for a first real type: bind genuine nix::StorePath from
the fetched Nix source (store path 2ijv0g6069dsh55z3bdr5ln2iv69mw7r),
keeping the mock alongside. Surfaces the last unknowns: real build
linkage, header quirks, namespace depth, boehmgc linkage against the
real library.

Gate: the review's verdict cluster (001/002/006) should be closed
first - runner lifecycle flaws convert from leaks to native crashes
against real libstore.

## Update 2026-08-25

The verdict cluster is closed: 001, 002 and 006 are all done, and 002's
escrow lease leak - found 2026-08-25 - is fixed with an invariant
check behind it.

Two NEW gates matter more than that cluster did, both blocking in
practice rather than in principle:

- 019 (typed Acquire). Real stores come from openStore(uri). An
  argument-less Acquire can construct nothing worth constructing
  against real libstore.
- 020 (binding-to-C++ link). The generator maps pxd types to Python
  ones by stripping a leading "C". Real headers will not cooperate.

021 (free functions) is not blocking but is where a lot of the real
libstore surface lives.

## Findings from reading the real source (2026-08-25)

Read while verifying 017's claim about StorePath. Store path
2ijv0g6069dsh55z3bdr5ln2iv69mw7r.

- `nix::StorePath() = delete`. Real Nix FORBIDS default construction.
  Our mock declares `StorePath() = default` "as binding glue", and
  store.pyx depends on it: every produced value does
  `StorePath.__new__(StorePath)` and then assigns `_ptr`. That pattern
  does not survive contact with libstore. The binding will need a
  different produced-value shape - a pointer that starts null and is
  set by a factory, or construction in place.
- StorePath holds one std::string. Its accessors return string_view
  INTO that member, so a Cython binding must copy before the owner
  dies, and must not hand a dangling view to Python.
- StorePath already has nlohmann JSON serialization upstream
  (adl_serializer, json_avoids_null). The `_wire = "value"` policy
  matches a category Nix already has, rather than inventing one -
  and there may be a real serializer to reuse instead of _parts().
- DerivedPath::to_string takes a `const StoreDirConfig &`. Wire-value
  serialization is not always a pure method of the value; some of it
  needs store context. _wire_fields assumes a self-contained
  _parts(); that assumption breaks here.
- The header layout is src/libstore/include/nix/store/*.hh, so the
  pxd include paths are "nix/store/path.hh" and friends.
