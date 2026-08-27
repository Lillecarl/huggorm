# The mock goes

**IN PROGRESS.** The plan of record for retiring `fake-library/` and
every `Mock*` binding, in the order the pieces can actually move.

## Why now

Carl: *"the only reason the mock existed was to have a small target to
experiment with, this was back when this project was still using
cython, now we're going all in on our own codegen and nanobind... the
goal is mapping Nix and in my opinion we're far enough along in mapping
Nix to use Nix as the target."*

The first answer to "why keep it" was that the mock is the only
exercise for five emitter features - `@derives`, `@abstract`,
`@virtual`, `threading="affine"`, `@tree`. That is true and it is
weaker than it sounds, because **the mock's shapes were designed to fit
the emitter.** A hierarchy the emitter handles, tested against a
hierarchy written to be handled, is close to a tautology. Real
`nix::Store` is where a shape can surprise us, and that is the test
this repo does not have.

Three other justifications are already dead:

- *hermetic testing without a daemon* - chroot stores do that, and
  most of the suite relies on it;
- *a store over RPC* - `client.acquire("Store", str(tmp_path))` works
  today;
- *a small thing to experiment against* - Carl's own point.

`fake-library/` is 881 lines across 7 files.

## The order, and why it is not the obvious one

**1. The lifecycle suite leases a real store.** DONE. Those tests are
about leases; the object behind the handle was incidental.

**2. Delete the trampoline machinery.** BEFORE the hierarchy, not
after, and this is the one that looks backwards.

The USER - a Python subclass of a store - is dead weight: nothing
wants a Python-implemented `nix::Store`, and upstream's registration
would not take one. The MACHINERY has two future users already on
file, 032 (nix::Logger) and 033 (primops). So neither keeping it warm
nor deleting it forever is right.

It goes now, with a pointer, because of the same tautology: the
current emission was shaped against a mock designed to fit it, and
Logger's real constraints - re-entry from arbitrary C++ threads, GIL
acquisition, bounded queues - will reshape it anyway. Keeping
toy-shaped machinery risks 032 inheriting the mock's shape. Version
control keeps the code; 032 names the commit.

**3. The store hierarchy.** `nix::Store` (abstract) ->
`nix::LocalFSStore` -> `nix::LocalStore`, each declared WHEN it has
methods of its own. A leaf with none stays undeclared.

The measurable outcome is a hatch count that goes DOWN, not more
classes. `real_path` today hatches a `dynamic_cast<LocalFSStore *>`
and a hand-thrown Unsupported - which IS the missing hierarchy,
written as a hatch. 040 deferred it "until a second LocalFSStore-only
method wants it", and real Nix brings `getFSAccessor` and
`addPermRoot` immediately; LocalStore adds `collectGarbage` and
`optimiseStore`. The leaves are where the dynamic_casts go to die.

`open_store` keeps returning the base. nanobind downcasts a
polymorphic pointer to the most-derived REGISTERED type on its own, so
`isinstance(store, LocalStore)` starts working the day LocalStore is
declared, with no change to the factory.

This kills `decl/mock_store.py` and the store half of fake-library.

**4. Real libexpr.** `nix::EvalState` and `nix::Value`, which is where
`threading="affine"` and `@tree` get a real user. This kills the rest
of fake-library, and `_cpp/eval.hpp`'s 108 lines get re-derived under
the number the census now prints.

`decl/eval.py` SURVIVES every earlier step. It is a declaration, not a
mock; what changes is which C++ it names. Every affine test, every
Realize test and every recursive-handle test rides mock eval until
step 4.

## The window where something is untested, named rather than hidden

**Between step 2 and step 4, proxy-produces-proxy has no test.** Not
just an untested test - untested MACHINERY. The server's
`parents=[self]` pinning, the cascade reap and the adopt path have
their only exercise through `MockRemoteStore.query_derivation` ->
`MockDerivation`.

Every real binding to date produces VALUES - PathInfo, Realisation,
MissingPaths, DerivedPathBuilt - so there is nothing to move it to.
`nix::EvalState` producing `nix::Value` is the first real pair.

`test_producer_pinning_and_cascade_reap` is **parked, not deleted**.
The test encodes the contract, and rewriting it from memory at step 4
would lose whatever detail made it sharp.

## Two properties that are load-bearing where the mock looked incidental

**Threading-policy diversity.** MockLocalStore is pool,
MockRemoteStore and MockDerivation are affine - so the lifecycle suite
had both policies on one hierarchy. Real Store is pool-only. A test
whose POINT is affine behaviour must move to mock EvalState/Value,
which is already affine, and not to a real Store: it would silently
become a pool test that still passes.

**The temp-roots flock.** `declare.py` records why nanopynix grew a
per-state-directory cache: two LocalStores in one process deadlock on
a temp-roots flock. The lifecycle suite acquires a store many times,
which is exactly that shape - so it holds `dummy://` stores, which are
in-memory and take no lock. A later "improvement" to a shared tmp_path
would HANG the suite rather than fail it, and the suite says so where
it would happen.

## The mechanical-swap lesson, in one sentence

A rename that matches on a VALUE reaches every coincidence of that
value. Replacing `== "local"` across the lifecycle suite reached the
mock EVALUATOR's store URI, which had nothing to do with the store
being swapped. The suite caught it immediately; a value-match will do
this every time.
