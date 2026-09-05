# A python primop that closes over its state leaks the state

**OPEN.** Opened as "a leak report rides every suite run", which is
how it was found. It is now diagnosed: an UNCOLLECTABLE reference
cycle through nix's own memory, and Python's collector cannot break
it.

Nothing is fixed. The fix is C++ and needs a per-occasion ask.

## What the suite says

    nanobind: leaked 4 instances!
     - leaked instance ... of type "huggorm_bindings.eval.EvalState"  (x4)
    nanobind: leaked 1 types!
    nanobind: leaked 18 functions!

The type and its functions leak BECAUSE the instances do: nanobind
cannot free a type while an instance of it lives.

Not `tasks/089`, and measured rather than assumed: the same four
appear with that task's four new tests deselected.

## Which four

Bisected by file, then by test. Exactly four tests leak, one each:

    test_primop::test_a_python_function_answers_as_a_builtin
    test_primop::test_the_arguments_arrive_forced
    test_function::test_python_calls_nix_calling_python
    test_function::test_a_python_primop_applied_through_a_lambda

Forty other tests in those two files leak nothing, and four of THOSE
also call `register_primop`. So it is not registration.

The discriminator is what the registered callable CLOSES OVER:

    leaks      lambda a, b: state.make_int(a.integer() + b.integer())
    leaks      def peek(v): ...; return state.make_int(...)
    clean      lambda v: v
    clean      lambda v: 42
    clean      def boom(v): raise ValueError(...)

Every leaking callable names `state`. Every clean one does not.

## The cycle

`Evaluator::register_primop` captures the callable BY VALUE as an
`nb::object`, which is a strong Python reference, and stores the
lambda in a `nix::PrimOp` that goes into the state's base
environment (`cpp/eval.hpp:679`). So:

    Python EvalState wrapper
      -> huggorm::Evaluator -> shared_ptr<EvalCore> -> nix::EvalState
      -> base env -> nix::PrimOp -> impl lambda
      -> captured nb::object fn -> the closure's cell
      -> Python EvalState wrapper

The `std::weak_ptr<EvalCore>` in that lambda breaks the C++ arm and
was clearly put there for this reason. It does not help: the arm that
closes the cycle is the PYTHON reference held from inside nix's
memory, and Python's cycle collector cannot traverse a C++ object.

## Reproduced, and one probe that lied

`.scratchpad/probe_primop_cycle.py` holds it. Two ingredients, and
both are needed:

    fixture, callable closes over state     leaked=1
    fixture, callable does not              leaked=0

**An earlier probe appeared to REFUTE this, and was wrong.** It did
`del state` before checking, and read "no leak" as "no cycle". But
the lambda closes over that same variable through a CELL, so `del
state` does not drop a reference - it EMPTIES the cell and breaks the
cycle by hand. The probe was undoing the thing it was measuring.

Recorded because the wrong reading was believed for two runs, and
because `del` looks like a neutral way to drop a reference and is not
when a closure shares the binding.

The decisive run leaves the binding alone and asks the collector:

    fixture, closes over state, no gc.collect()    leaked=1
    fixture, closes over state, gc.collect() x2    leaked=1

So the cycle is UNCOLLECTABLE, not merely uncollected.

## Why it matters beyond a shutdown warning

The destination is a service that runs for days. A leaked `EvalState`
is not a warning - it holds a store, its GC roots and its affine
thread's registration, for the life of the process. A caller that
registers one Python primop closing over its own state can never
reclaim that state.

And closing over the state is the NATURAL way to write one: a primop
has to build its result with `state.make_int`, so the useful callables
are exactly the ones that leak. The clean ones in the suite are clean
because they return their argument or raise.

## What a fix would take

C++, and a per-occasion ask. The candidates, best first:

1. **Let Python's collector see the callable.** nanobind can give a
   bound type `tp_traverse` and `tp_clear` through `nb::type_slots`,
   so the wrapper reports the callables it holds and the cycle becomes
   collectable. It needs the `Evaluator` to KNOW its callables - today
   they live only inside lambdas in nix's base env - so it is a list
   on `EvalCore` plus two slot functions. This is the real fix.
2. **Hold the callable weakly.** Smaller, and it changes the contract:
   a callable nobody else holds would die and the primop would fail at
   call time. That trades a leak for a surprise.
3. **Refuse a callable that closes over its state.** Not possible to
   detect honestly, and it would forbid the useful case.

Option 1 first. Before writing any of it, measure what nanobind
actually offers here rather than trusting this paragraph - the API is
recalled, not read.

## The gate this wants

A test that registers a state-closing primop, drops every Python
reference WITHOUT `del` on the closed-over name, collects, and asserts
the state was freed. Today there is nothing: the leak is reported at
interpreter shutdown, after the last test, so no test can see it.

`weakref` is not the tool - `huggorm_bindings.eval.EvalState` has no
weakref slot ("cannot create weak reference"). So the gate needs
either that slot or a canary object the state owns.

Opened 2026-09-05, from a suite run for `tasks/089`.
