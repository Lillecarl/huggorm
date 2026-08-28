# The mock goes

**DONE.** `fake-library/` is deleted and no binding names a mock.
This is the record of how it went, and of what the mock was
flattering us about.

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
DONE. Nothing had to replace the hierarchy first.

Carl, while this was running: *"I haven't had any usecase so far for
downcasting to the store implementation classes... nanopynix has never
exercised it."* So the hierarchy is not merely blocked, it is not
wanted. `real_path` keeps its `dynamic_cast` and that is the end of
it.

**But the question the hierarchy test posed outlives the test.** When
a method DECLARES a return of `Store` and produces a `LocalStore`,
what class does the RPC client build - the dynamic type at
production, or the declared static type? Nothing answers this today
because nothing registers a second store type. Whoever declares one
must answer it before the first such method ships.

**Five properties lost their only exercise, and all five are named
rather than hidden.** Three died with the hierarchy and are wanted
back only if a real base lands:

- a generated base whose subclasses share one wire service;
- the pool policy DROPPING an affine-returning method from a pool
  wrapper while an affine wrapper keeps it;
- an abstract binding refusing construction. `@abstract` now has no
  user at all, and cannot get one until it stops meaning two things
  (tasks/061). The branches stay in `nbemit`, `emitter.py` and
  `smoke_test` - waiting, not dead.

Two come back one commit later, with the libexpr `EvalState`:

- a FREE FUNCTION taking a bound handle. `describe(obj: MockStore)`
  was the only one this repo had;
- an UNTOUCHED AFFINE handle resolved as an argument.

`EvalState(store)` restores both at once: a factory taking a bound
Store handle, making an affine object.

**5. Real libexpr.** DONE. `decl/eval.py` names `nix::EvalState` and
`nix::Value`. The anchor reads came first, and one reviewer premise
reversed - see "What the headers actually say" below.

Four things the mock was flattering us about, each found by breaking
a gate rather than by reading:

- **`nix::Bindings` is sorted by Symbol ID**, which is INTERNING
  order - the order a name was first seen anywhere in the process -
  not alphabetical. `lexicographicOrder` is the accessor that hides
  it. Caught as `['zebra', 'apple', 'mango']`.
- **`nix::Value` is a tagged union whose readers are `noexcept` and
  UNDEFINED on the wrong tag.** Reading `integer()` off a string is a
  reinterpretation of the payload, not an error. So no accessor can
  bind a pointer-to-member and every one guards. `via` had its only
  user here and lost it - refuted rather than merely unused.
- **A `nix::EvalState` owns every expression it parses for its whole
  life** (`eval.hh:351`). Dropping a parsed thunk frees the 16-byte
  Value and nothing else.
- **Real libexpr is fast.** The affine-serialization gate required
  two gathered evals to take twice as long as one, which worked
  because the mock SLEPT. It counts workers now: a thread name is not
  a stopwatch.

And one thing that was NOT the mock's shape after all. The mutable
builder - `make_list()` then `list_append` - looked like a toy, and a
Nix collection really is immutable and sized at build. But
`make_list(items: list[Value])` needs a container of PROXIES to
cross, and one lease per element is not something anything grants in
bulk, so it would have no RPC surface at all. One element per call is
what the WIRE requires. It stays.

## What is left over

**The builder is quadratic in C++.** One element per call is the wire
shape; it does not force one Nix-list rebuild per call. The Bridge
could accumulate in a vector and materialise the immutable list once,
on first read. The elements would need rooting as they enter, since a
plain vector is memory boehm does not scan.

**`via` should be deleted.** Its one user proved the mechanism unsafe
for this library, which is a stronger reason than being unused. A
vocabulary word that must not be used is worse than one that does not
exist, and version control is where refuted ideas live. `@abstract`
is different: it is WAITING on the 061 split, and states something
true about `nix::Store` that cannot be said today. `nix::EvalState` and `nix::Value`, which is where
`threading="affine"` and `@tree` get a real user. This kills the rest
of fake-library, and `_cpp/eval.hpp`'s 108 lines get re-derived under
the number the census now prints.

`decl/eval.py` SURVIVES every earlier step. It is a declaration, not a
mock; what changes is which C++ it names. Every affine test, every
Realize test and every recursive-handle test rides mock eval until
step 4.

## What the headers actually say

Read from `nix-2.34.8-dev/include/nix/expr`, before writing a line -
the repo's anchoring rule, and this was its moment.

**`@tree` survives, and the worry about it was misplaced.** The fear
was that a walk serializes a WHOLE tree and dies on `import <nixpkgs>
{}`. It does not. `TreeWalk` (server.py) is bounded by depth AND
budget - 8 and 1000 by default - dedupes by the DECLARED identity
accessor, and every stop produces a PROXY: an unnamed kind, past the
depth, past the budget, or already seen. `import <nixpkgs> {}`
expands at most 1000 nodes and hands back handles for the rest. It is
the handle shape already, in one hop rather than one per node, which
is the thread handover the affine model exists to avoid paying
repeatedly.

**The walk never forces, and that is deliberate.** `thunk` is named
nowhere in the scalars/list/attrs table, so it falls through to the
proxy arm. Forcing is `EvalState.force`, explicit and `@blocks`. Real
`nix::ValueType` adds `nThunk`, `nFailed`, `nFunction` and
`nExternal` - four more kinds the walk will not name, which is four
more proxies and no declaration change. `nFloat`, `nPath` and `nNull`
are new SCALARS and want three rows in the table.

**`nix::Value` is NOT self-describing, and this is the real finding.**
An attribute name is a `Symbol`, a `uint32_t` index into the producing
`EvalState`'s `SymbolTable`. Rendering one needs the state in hand,
and the tree spec calls accessors on the NODE (`getattr(obj,
reader)()`).

It costs no emitter change, because the bound class was never the
value. `via="get()"` says the binding is `cythonix::Bridge`, a handle.
The Bridge carries a reference to its `EvalState` beside the value
pointer, and `name_at(i)` is a Bridge method with the state in hand.
The Bridge holding the state is producer pinning IN C++, matching the
server's `parents=[self]` exactly.

**The hand-rolled GC root goes.** `nix::allocRootValue(Value *)`
exists in 2.34 - `value.hh:1444`, returning `RootValue =
std::shared_ptr<Value *>` built over `traceable_allocator`
(`eval.cc:105`). That is what `_cpp/eval.hpp`'s
`GC_malloc_uncollectable` cell hand-rolls. Use upstream's and delete
ours: a hand root beside an upstream root is a double declaration.

What stays ours is thread registration. Boehm only knows threads it
created, and pool threads are not among them.

`nix::initGC()` is the `@startup` function, once before the first
state. `EvalMemory::allocValue()` means the Bridge never MAKES a
value - it only points at one - so it shrinks rather than grows.

**The factory takes our Store.** `EvalState(const LookupPath &,
ref<Store>, const fetchers::Settings &, const EvalSettings &,
std::shared_ptr<Store> buildStore = nullptr)`. Both settings objects
"must outlive the lifetime of this EvalState" - upstream's own words
- so one owner cell in `_cpp/eval.hpp` holds settings and state
together rather than scattering globals. And the store is OUR bound
handle's `shared_ptr`, not a second store opened inside the factory.

## The window where something is untested, named rather than hidden

**Between step 2 and step 4, proxy-produces-proxy has no test.** Not
just an untested test - untested MACHINERY. The server's
`parents=[self]` pinning, the cascade reap and the adopt path have
their only exercise through `MockRemoteStore.query_derivation` ->
`MockDerivation`.

Every real binding to date produces VALUES - PathInfo, Realisation,
MissingPaths, DerivedPathBuilt - so there is nothing to move it to.
`nix::EvalState` producing `nix::Value` is the first real pair.

CLOSED. `test_producer_pinning_and_cascade_reap` was re-pointed at
`EvalState` -> `Value` BEFORE the store mock went, so the window never
opened. A Value is GC memory the state owns, which makes a state
reaped under a live value a use-after-free rather than a lost handle -
a sharper subject than the mock ever was.

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
