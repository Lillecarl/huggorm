# Registering primops from Python

**OPEN.** No primop can be registered from Python. `eval.py` binds the
evaluator, not its extension points.

Carl, 2026-08-25: "Nix allows registering primops. These should be
implementable in Python."

## What upstream is

Verified against src/libexpr/include/nix/expr/{eval,primops}.hh in
/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source.

    struct PrimOp {
        std::string name;
        std::vector<std::string> args;    // arity derives from this
        size_t arity = 0;
        std::optional<std::string> doc;
        bool addTrace = true;
        fun<PrimOpFun> impl;
        std::optional<ExperimentalFeature> experimentalFeature;
    };

    struct RegisterPrimOp { RegisterPrimOp(PrimOp && primOp); };

`impl` is `(EvalState &, PosIdx, Value ** args, Value & v)` - it
receives the state, a source position, an argument array and an
out-parameter to fill.

Two registration routes: the global `RegisterPrimOp` list, consumed
during EvalState construction, and per-EvalState registration.

## Why this is the interesting one

Everything so far has been Python calling into C++. This is C++ calling
into Python, on Nix's terms, in the middle of evaluation. The
trampoline pattern covers it (PyStore already forwards a pure virtual),
but three things here are genuinely new.

**Threading.** A primop runs on whatever thread is evaluating, holding
whatever locks the evaluator holds, and it re-enters Python there. The
whole point of `_threading = "affine"` on EvalState is that its work
stays on one thread - so a primop implemented in Python runs on the
EvalState's own thread, and it must not hop, block on a lock the
evaluator holds, or await. It is the exact inverse of every wrapper the
codegen emits, which exist to get OFF the calling thread.

That also means a Python primop cannot do async work. It could hand
back a thunk that a later force resolves, but that is a design, not a
detail.

**Errors.** A Python exception raised inside a primop is in the middle
of a C++ evaluation. It has to become a Nix `EvalError` with position
information, not propagate as a Python exception through C++ frames.
The typed-error machinery (WrapperError.to_dict) is the right shape;
what is missing is the position, which only the primop's PosIdx knows.

**Value lifetime.** `Value ** args` and `Value & v` are GC-managed and
owned by the evaluator, not by Python. The existing bridge-cell trick
(GC_malloc_uncollectable, freed in __dealloc__) is for values Python
holds ACROSS calls. A primop's arguments live exactly as long as the
call, so wrapping them in a Python object that outlives it is a
use-after-free. The binding needs a borrowed view that refuses to
outlive the call, which is not a thing this repo has yet.

## Fit with the surface

`name`, `args` and `doc` are exactly what the generator already
produces for a function, so a Python primop declared with the existing
markers could emit its own registration. That is the appealing version:
declare a function in Python, get a `builtins.<name>` in the evaluator.

Registering across the WIRE is a different question and probably a no:
a primop is a callback into the registering process, so a remote client
registering one means the evaluator calls back over the socket on its
own evaluation thread, per invocation. That is a distributed
call-in-a-hot-loop, and it deserves a decision rather than falling out
of the generator.

## Depends on

015 (the real-Nix spike) - the mock has no evaluator to register into,
so this needs real Nix or a mock primop table built to match. A mock
version is worth it: the threading and lifetime rules above are what
need proving, and none of them need real Nix to be wrong.

## Dual of tasks/034

034 is the same boundary from the other side: a Nix function called
from Python. The threading rules are opposite and neither can borrow
the other's shape. A primop runs inside evaluation, so it is SYNC and
must not await. Applying a Nix function hops onto the evaluator's
thread, so it is ASYNC and must not be anything else.

## 2026-09-03: three facts read from 2.34.8, and one of them is a wall

Read from the headers this build links and from Nix's own source,
before designing anything.

### `fun<PrimOpFun>` is a `std::function`, so a lambda may capture

`nix/util/fun.hh:23` - `fun<Ret(Args...)>` holds a
`std::function<Ret(Args...)>` and only refuses a null one. It takes
any callable `std::function` accepts.

That settles the helper's shape and it is the good answer. A capturing
lambda holding an `nb::callable` goes straight in, so there is no
registry keyed by name, no trampoline class, and no static table. Had
it been a raw function pointer there would be no user-data slot and
all of that would be forced.

### Registration is PRIVATE, and the shim for that already exists

`EvalState::addPrimOp(PrimOp &&)` is at `eval.hh:837`, under the
`private:` at 828. So per-EvalState registration is not public surface
in this version.

That is the same situation as `fileEvalCache`, and `cpp/eval.hpp`
already holds the answer: the `Reach` template instantiated with the
member pointer, by [temp.spec]/6. A third tag, and no patch.

### The wall: the base environment is 128 slots and 119 are taken

`addPrimOp` ends with (`eval.cc:549`):

    staticBaseEnv->vars.emplace_back(envName, baseEnvDispl);
    baseEnv.values[baseEnvDispl++] = v;

`baseEnv` is `mem.allocEnv(BASE_ENV_SIZE)` with
`BASE_ENV_SIZE = 128` (`eval.cc:228, 311`), and `allocEnv` is
`allocBytes(sizeof(Env) + size * sizeof(Value *))` - a flexible array
of exactly 128 pointers (`eval-inline.hh:88`).

**There is no bounds check in `addPrimOp`.** Measured against the
same 2.34.8 this build links:

    nix-instantiate --eval -E 'builtins.length (builtins.attrNames builtins)'
    119

    builtins ? builtins   ->  true      (so `builtins` is one of them)
    __-prefixed names     ->  0         (stripped, as the source says)
    .internal = true      ->  0         (none in this primops.cc)

Every one of those 119 consumed a slot. So roughly NINE remain, and
the tenth registration writes past the end of a GC allocation. Not an
exception, not a refusal - a heap overflow into the collector's
memory, which is this repo's named failure mode in its worst form.

So "register a primop" cannot be an open-ended API on this version.
Whatever ships either counts the slots and refuses, or does not touch
the base environment at all.

### What the previous plan assumed and no longer holds

This file says "the trampoline pattern covers it (PyStore already
forwards a pure virtual)". `tasks/060` deleted every trampoline with
the mock, and `nbemit.py:2063` says so in the code: "There are no
trampolines any more". `tasks/032` rests on the same sentence.

So the shape both tasks called "the easy part" is not in the repo.
What replaces it is smaller, not bigger - `fun` takes a lambda, so
nothing needs to be subclassed - but it is the first C++-calls-into-
Python path since the mock died, and nothing existing demonstrates
the `gil_scoped_acquire` half of it.

## 2026-09-03: the wall is patched, not worked around

Carl's answer to the design question the measurement raised:
"/home/lillecarl/Code/nanopynix patches the slots to 512, steal those
patches. There are tiny patches required to make good bindings for
now, eventually I'll work on upstreaming dynamic env sizing."

So `nix/patches/nix-base-env-size.patch` is carried here, and
`default.nix` applies it with `pkgs.nix.appendPatches`. None of the
four shapes the measurement suggested is needed: registration can be
an ordinary API again.

### What the patch found that the measurement above MISSED

**There are two containers of 128, not one.** `createBaseEnv` also
builds the `builtins` attribute set with `buildBindings(128)`, and
both `addConstant` and `addPrimOp` push into it through a
`const_cast` that goes around `BindingsBuilder`'s capacity assert.
`Bindings::push_back` is `attrs[numAttrs++] = attr;` and holds no
capacity to test at all.

Raising only `BASE_ENV_SIZE` would have moved the corruption from
`allocEnv` to `Bindings::push_back` rather than removing it. The
measurement recorded above found the base environment and stopped
there, so it would have produced a fix that looked right and was not.
Both sizes go to 512 together.

**The collector HIDES the overflow.** `allocBytes` is `GC_MALLOC` and
Boehm rounds a request up to a size class, so the out-of-bounds write
lands in the block's slack and nothing reports it. A build with
`-Dgc=disabled` gets an exact `calloc` and glibc aborts with
"corrupted size vs. prev_size". Both builds make the same write; only
one of them says so. That is why the defect survived - and it is why
the headroom measured above is a real wall rather than a soft one.

nanopynix caught it under AddressSanitizer, with the frame named:
`addPrimOp` -> `createBaseEnv` -> the `EvalState` constructor, 0 bytes
after a 1032-byte region.

### Why the size is a constant at all

Worth keeping, because it says what upstreaming would take. `Env` is
variable-length, the parser compiles each reference to a (level,
displacement) pair, and `baseEnv` is a REFERENCE member allocated in
the constructor's member-initialiser list - before the body runs, and
so before `createBaseEnv` has counted anything. The size is a constant
for want of a count, not for a property of the evaluator. Moving the
allocation into the constructor body and taking the size from the
registry is the upstream fix, and it is Carl's eventual plan.

### One patch, one version, on purpose

nanopynix keys a patch table by `majorMinor` and builds a scope per
version, because it supports 2.34 through git. This repository binds
whatever `pkgs.nix` is and carries one file.

Carl, the same day: nanopynix is production ready, "this is still an
elaborate spike". A version matrix costs maintenance and buys nothing
until there is a second version to serve. The patch header records
that the three hunks have identical context in 2.31, 2.34 and 2.35,
so a bump moves line numbers only - and if it ever stops applying,
that failure is the signal to read it again rather than to add a
matrix.

## 2026-09-03: the borrowed view is not needed, and the cycle is

Two more things read from this repo's own C++ rather than assumed.

### "Value lifetime" above is WRONG, and the design shrinks

This file says:

> A primop's arguments live exactly as long as the call, so wrapping
> them in a Python object that outlives it is a use-after-free. The
> binding needs a borrowed view that refuses to outlive the call,
> which is not a thing this repo has yet.

`Bridge` already answers it. Its constructor is
`root_(nix::allocRootValue(value))` (`cpp/eval.hpp:455`) - EVERY
Bridge takes its own GC root, which is the whole reason the class
exists. A Bridge over a primop argument keeps that argument reachable
for as long as Python holds it.

So there is no use-after-free to defend against, and no borrowed-view
type to invent. The cost of holding one is RETENTION - an argument
Python keeps is a value the collector cannot take - which is the same
cost every other value crossing this boundary already has, and is a
leak at worst rather than a crash.

That deletes a subsystem from the plan. A primop takes `Value`
proxies and returns one, exactly like every other binding here, and
nothing about it is special.

### The cycle is the real lifetime problem

A `Bridge` needs a `std::shared_ptr<EvalCore>`, and `EvalCore` owns
the `EvalState` the primop is registered on. A callback capturing
that shared_ptr would make the state own a callback that owns the
state, so neither is ever freed - a leak that no test asserting on
`live_roots()` would see, because no root leaks.

A `weak_ptr<EvalCore>`, locked inside the callback, breaks it. An
expired weak pointer means the state is being destroyed, and a primop
belonging to a destroyed state cannot be running - so the failure is
unreachable rather than merely unlikely, and it should say so if it
ever fires.

Written down before any C++ exists, because it is the kind of defect
that compiles, passes, and is found months later by a server that
grows.

### The patch applied, and what that does NOT yet prove

`pkgs.nix.appendPatches` rebuilt all five components as `2.34.8+1`,
and the three hunks are in the source this build links
(`ippimy5...-source-patched-source`):

    src/libexpr/eval.cc:228    BASE_ENV_SIZE = 512
    src/libexpr/eval.cc        2x "the base environment is full"
    src/libexpr/primops.cc:5151 buildBindings(512)

`all checks passed`, 287 passed. So the patch costs nothing that this
suite can see, which is the only claim available today.

**It is UNGATED, and will stay so until registration exists.** The
patch has no observable effect from Python: `builtins` still holds
119 names, and nothing here registers a primop, so no test can tell a
patched evaluator from a stock one. The gate arrives with the binding
- register past 128 names and read an error instead of corrupting the
heap - and that gate is the reason the patch is here at all.

Said plainly rather than left implied, because "the build is green"
is not evidence about a bound nothing reaches yet.
