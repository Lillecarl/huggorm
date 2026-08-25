# Registering primops from Python

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
