# Free functions and module-level bindings have no generated surface

Found in the 2026-08-25 review.

## Problem

The stated goal is complete surface coverage generated from the
binding specifications. Free functions are outside it entirely.

pxd.py already parses `describe_store` into api["free_functions"].
generate.py reads only api["classes"] and throws the rest away. So the
parse result is dead data.

Three module-level bindings have no async wrapper, no manifest entry
and no RPC surface: `describe(obj)` (the C++ virtual-dispatch
trampoline demo), `gc_stats()` and `collect_garbage()`. A remote
client cannot ask the server for collector counters or trigger a
collection - the two things a long-lived evaluation server (016) most
obviously needs.

## Why it matters

Real libstore and libexpr are full of free functions: openStore,
parseStorePath, computeFSClosure. If free functions stay outside the
generator, a large part of the real surface is hand-written forever,
which is exactly what this design exists to avoid.

## Direction

- A module-level function needs the same policy markers a class
  carries. Function attributes work in Python
  (`collect_garbage._threading = "pool"`) but read badly. Consider a
  module-level registry in the pyx, or a decorator the codegen reads.
- collect_garbage is process-global and blocking, not a method on
  anything. Its threading policy is "pool", and the async wrapper
  should be a module-level coroutine, not a class.
- gRPC has no free functions: they need a service to live on. A
  per-module service (`fake_libraryService`) is the obvious shape.
- describe(obj) takes a proxy argument and returns a scalar. The
  existing WireCodec handles that unchanged.

Blocks nothing today; 016 will want gc_stats over the wire.
