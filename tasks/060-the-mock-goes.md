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

**2. Delete the trampoline machinery.** DONE. BEFORE the hierarchy, not
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

**3. The store hierarchy - BLOCKED, and the reason is nanobind's.**

The plan was `nix::Store` (abstract) -> `nix::LocalFSStore` ->
`nix::LocalStore`, each declared when it has methods of its own, with
`real_path`'s `dynamic_cast<LocalFSStore *>` hatch dying into the
type system. It rested on nanobind downcasting a polymorphic pointer
to the most-derived REGISTERED type.

It does not do that. It downcasts to the most-derived type whose
`typeid` is registered EXACTLY - `nb_type.cpp:2140` looks up
`&typeid(*ptr)` in the registry and falls back to the STATIC type
when it misses. There is no walk up the base chain.

Verified rather than reasoned: `LocalFSStore` was declared and bound,
and `Store(tmp_path)` still came back as `Store`. The runtime type of
a chroot store is `nix::LocalStore`, which was not registered, so the
intermediate registration bought nothing - nothing is ever an
INSTANCE of `LocalFSStore`.

Making the downcast work means registering every CONCRETE store Nix
has - LocalStore, LocalOverlayStore, UDSRemoteStore,
HttpBinaryCacheStore, S3BinaryCacheStore, DummyStore... - and missing
one makes `real_path` silently vanish for stores that have it. That
is worse than the hatch: the hatch's `Unsupported` is honest and
uniform.

So `real_path` KEEPS its `dynamic_cast`. It is not a hatch standing in
for a hierarchy; it is doing something the type registry cannot. 040
deferred the hierarchy waiting for a second method to want it, and the
real blocker turns out to be different and further away.

What this does NOT block: `decl/mock_store.py` still dies, because
nothing needs a hierarchy to replace it - the mock's hierarchy was
only ever exercising `@derives`/`@abstract`, and those keep working
against whatever declares them next.

If it is ever wanted, the shape is known: declare the CONCRETE stores
and accept that an unregistered one degrades to the base. That is a
decision about how much of Nix's store zoo this repo tracks, and it
should be made for its own reasons rather than to retire a hatch.

**4. `decl/mock_store.py` and the store half of fake-library go.**
Nothing has to replace the hierarchy first: the mock's hierarchy was
only ever exercising `@derives`/`@abstract`, which now work against
whatever declares them next.

**5. Real libexpr.** `nix::EvalState` and `nix::Value`, which is where
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
