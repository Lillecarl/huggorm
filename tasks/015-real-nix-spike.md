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
