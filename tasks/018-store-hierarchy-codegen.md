# Store hierarchy: one ABC, shared surface through every layer

Carl's call (2026-08-23): Store/LocalStore/RemoteStore share
practically all API surface - Store IS the ABC, mirroring upstream
Nix. Today that shape survives C++ -> pxd (CLocalStore(CStore)) but
dissolves above it: emitted Async* classes are flat and standalone,
and the gRPC layer emits LocalStoreService/RemoteStoreService as
unrelated duplicates of the same method list.

## Goal

Model the base-class relationship once and let every layer derive
from it: sync bindings, generated async wrappers, manifest, gRPC
schema. Concrete subclasses add or restrict; nothing duplicates.

## Design questions

- Emitted AsyncStore as a real base carrying the hop-method bodies
  (they are identical: self._runner.call(...)); subclasses contribute
  __init__/runner and their POLICY-FILTERED surface. Tension to
  resolve: LocalStore is pool, RemoteStore is affine, and policy
  drops diverge (query_derivation exists only on RemoteStore).
  Inheritance plus per-subclass hiding needs a clean mechanism -
  candidates: base holds everything and subclasses shadow-drops, or
  emitter flattens per subclass but VALIDATES against the shared set.
- gRPC: proto has no inheritance. Keep per-concrete services but
  generate their method sets from the deduplicated Store surface;
  consider whether reflection clients should see a StoreService.
- Manifest gains explicit bases/borrowed-from metadata so the
  protocol layer (017) and external tools see the true shape.
- Upstream alignment: real Nix has an abstract refcounted Store base;
  getting this right now makes the 015 spike a substitution, not a
  rewrite.

Depends on nothing open; synergizes with 017 and 015.
