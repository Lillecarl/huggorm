# Real-Nix binding spike

Swap the mock for a first real type: bind genuine nix::StorePath from
the fetched Nix source (store path 2ijv0g6069dsh55z3bdr5ln2iv69mw7r),
keeping the mock alongside. Surfaces the last unknowns: real build
linkage, header quirks, namespace depth, boehmgc linkage against the
real library.

Gate: the review's verdict cluster (001/002/006) should be closed
first - runner lifecycle flaws convert from leaks to native crashes
against real libstore.
