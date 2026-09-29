# A Nix without the collector

**DONE.** Carl, 2026-09-29, choosing between porting and dropping
nanopynix's `-nogc` and `-asan` lanes when nanopynix drops
nanopynix_bindings: "Port huggorm to no-GC". Both lanes run a libexpr
built with `-Dgc=disabled`, and huggorm called Boehm directly.

## Done

- `cpp/gc.hpp` includes `nix/expr/eval-gc.hh`, which gives
  `NIX_USE_BOEHMGC` and brings `gc.h` in only when it is set. Every
  Boehm call sits behind it. Registration then has nothing to do, and
  a question only the collector can answer (`collect_garbage`,
  `gc_stats`) raises `UnimplementedError`, as nanopynix_bindings raised
  its own error: a map of zeros would read as a measurement.
  `Value.is_gc_managed` answers False, which is true.
- `nixVersions.nix_2_34-nogc` builds libexpr with `enableGC = false`.
  The bindings then link no libgc, so `boehmgc` may be null.
- The suite asks the BUILD whether it has a collector
  (`HUGGORM_NIX_GC`, from the bindings' `hasCollector`), never
  `boehm_gc()`, which is the binding under test. Nine collector tests
  carry `needs_collector`; `test_settings` holds that the probe agrees
  with the build and that a build without one refuses by name.
- The smoke gate in `huggorm-generated` asserts the collector facts
  where one exists and the refusal where none does.

2.34 with the collector: 641 passed, 1 skipped. Without: 632 passed,
10 skipped.

## Not here

The sanitizer lanes need no huggorm knob: nanopynix adds its flags to
the bindings with `overrideAttrs`, as its own bindings package did.
The wheel lane goes with nanopynix_bindings, Carl's call, and comes
back on huggorm later.
