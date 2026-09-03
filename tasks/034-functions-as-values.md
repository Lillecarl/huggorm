# A value can be a function

**MOSTLY DONE.** A function value applies both ways, introspects, and
describes itself as an `inspect.Signature`. `__call__` is what is
left, and it is a DSL gap rather than a decision. The section at the
end says what was built, what three docstrings got wrong, and what
breaking it proved.

Carl aborted the MOCK version of this to link real Nix (tasks/015),
which removed the reason it was blocked rather than the work - and
also removed the first bullet of the plan below, since `tasks/060`
deleted the mock entirely.

Carl, 2026-08-25: "Values can be functions as well. Can we return
Callable/Coroutine (sync/async) and which types are most appropriate
to be able to introspect in Python to be able to call cleanly?"

## What a function value is upstream

Verified against src/libexpr/include/nix/expr/{value,nixexpr,eval}.hh
and src/libexpr/eval.cc in
/nix/store/2ijv0g6069dsh55z3bdr5ln2iv69mw7r-source.

`nFunction` is one type name covering three runtime shapes, told apart
by `isLambda()`, `isPrimOp()` and `isPrimOpApp()` - the last being a
primop that has some of its arguments already.

A lambda is `Value::Lambda { Env * env; ExprLambda * fun; }`, and the
callable shape lives on the expression:

    struct ExprLambda : Expr {
        PosIdx pos;
        Symbol name;
        Symbol arg;                              // `x:` form
        std::optional<Formals> getFormals() const;  // `{ a, b ? d, ... }:`
        Expr * body;
        DocComment docComment;
    };

    struct Formal { PosIdx pos; Symbol name; Expr * def; };

`def` non-null means that formal has a default. `Formals` also carries
`ellipsis`, the `...` that lets a caller pass names the lambda does not
mention.

Applying:

    void callFunction(Value & fun, Value & arg, Value & vRes, PosIdx pos);
    void callFunction(Value & fun, std::span<Value *> args, ...);
    void autoCallFunction(const Bindings & args, Value & fun, Value & res);

`autoCallFunction` is the by-name path: it fills each formal from the
attrset it is given and falls back to that formal's default. It is what
`--arg` and `--argstr` reach.

And there is already an introspection primitive:

    struct Doc { Pos pos; std::optional<std::string> name;
                 size_t arity; std::vector<std::string> args;
                 const char * doc; };
    std::optional<Doc> getDoc(Value & v);

Read `getDoc` before relying on it. For a PRIMOP it is exactly a
signature - name, arity, argument names, documentation. For a LAMBDA
it builds a prose string for the REPL's `:doc` and leaves `arity` at 0
with `args` empty. A lambda's callable shape has to come from
`ExprLambda` instead.

## What that means for the Python surface

**Curried, so there is no arity.** `x: y: body` is a function that
returns a function, and nothing can say how many arguments it takes
without applying it. Only a primop declares one. Any surface that
offers a single `arity` for a lambda is lying.

**Two calling conventions, not interchangeable.** `f x` applies one
argument; `f { a = 1; b = 2; }` applies an attrset to a lambda that
declared formals. In Python those are `await f(x)` and
`await f(a=1, b=2)`, and the second is only meaningful when the lambda
has formals. The by-name form is also one round trip for a whole
argument set, where the curried form is one per argument - which is
the ergonomic argument for supporting both.

**Coroutine, not Callable.** Applying a function runs the evaluator, so
it happens on the EvalState's affine thread and it can block. So
`async def __call__`, returning a value that may itself be callable. A
sync `__call__` cannot exist in the async surface and cannot exist at
all over RPC; the only place one belongs is the raw binding layer,
which is sync throughout already.

Note the asymmetry with tasks/033. A primop implemented in Python must
be SYNC - it runs inside evaluation and cannot await. So Nix calling
Python is sync and Python calling Nix is async. They are duals with
opposite threading, and neither can borrow the other's shape.

**Static types cannot say much, and should not pretend.** Nix is
dynamically typed, so the honest annotation is a callable returning a
value: parameters unknown, result a Value. Everything sharper is
runtime.

**`inspect.Signature` is the answer to "introspect cleanly".** It is
the type Python's own tooling already understands - `help()`,
`inspect.signature()`, an IDE's call hints - and the formals map onto
it exactly:

- a formal becomes a KEYWORD_ONLY parameter, with `default` set when
  `def` is non-null and no default otherwise;
- `ellipsis` becomes a VAR_KEYWORD parameter;
- a simple `arg:` lambda becomes one POSITIONAL_ONLY parameter with
  the declared name;
- a primop becomes `arity` POSITIONAL_ONLY parameters named from
  `Doc::args`, with `Doc::doc` as the docstring.

Do NOT attach it as `__signature__` eagerly. Building one needs a
round trip, and a proxy is constructed for every function value that
crosses; paying for introspection nobody asked for would make
realizing a tree of functions quadratic. An explicit `await
f.signature()` returning an `inspect.Signature`, plus `await f.doc()`,
keeps the cost where the caller put it.

## It already crosses correctly

Nothing needs changing in the wire for this. The tree walk names no
function kind, so a function falls through the rule that says an
unnamed kind stays where it is, and it crosses as a proxy - which is
the only thing a function CAN be over a wire. Realize needs no change.

## What it would take

- The mock needs a function kind. There is no expression language to
  parse a lambda from, so a function has to be BUILT like a list or an
  attribute set. The interesting question is what its body is: a C++
  builtin (enough to prove currying, partial application and
  introspection) or a Python callable (which is tasks/033 and drags
  the whole callback story in). Start with the C++ one.
- `Value` gains `apply` and the two introspection reads. All three go
  through the existing declaration machinery: `_tree` already names a
  kind-reading accessor, and a `_callable` declaration beside it can
  name the apply and signature accessors the same way, so no layer
  above the binding learns what a function is.
- The formals need a representation that crosses. A name, a
  has-default flag, and the ellipsis flag are scalars and a bool, so
  this needs no new message - a small repeated one beside the value
  message is enough. The DEFAULT VALUE itself is an unevaluated
  expression, so it cannot cross as anything but a proxy, and probably
  should not cross at all: `inspect.Parameter` only needs to know that
  a default exists.
- Errors already work. Applying a non-function, or missing a required
  formal, is an EvalError, and the typed-error path carries it.

## Depends on, and blocks

Needs the mock work above. Independent of tasks/015, though real Nix is
where the interesting functions live.

Pairs with tasks/033: that one is Python code called BY the evaluator,
this one is evaluator code called by Python. Doing either first makes
the other's threading rules obvious by contrast.

## Deferred 2026-08-25, before any of it was built

Carl: "At this point it feels like we're implementing quite a lot of
Nix. Maybe it's better if we abort 034 and start linking against real
Nix?"

Correct, and the mock work had already started when he said it: a
builtin table, currying, partial application, formals with defaults
and an ellipsis. All of it real Nix has, none of it this repo needs to
invent. It was discarded.

What survives is the analysis above, which cost nothing to keep and is
about the PYTHON surface rather than the mock: coroutine not callable,
inspect.Signature built from the formals, fetched on request, and the
sync/async inversion against tasks/033. Every one of those still
applies against libexpr, and against libexpr the formals are real.

Pick this up after tasks/015. The one design decision already made and
worth keeping: applying lives on EvalState, not on Value, because it
runs the evaluator - the same reason force lives there. Which means a
bare `await f(x)` needs the value to know its state, and that is the
question to answer with a real ExprLambda in hand.

## 2026-09-03: built

**MOSTLY DONE.** A function value applies, introspects, and describes
itself as an `inspect.Signature`. What is missing is `__call__`, and
the reason is a DSL gap rather than a decision - the last section says
so.

Thirty-four gates in `tests/test_function.py`, and the suite went from
336 to 370.

    Value.apply(arg)          f x, the curried form
    Value.apply_auto(attrs)   autoCallFunction, by name, fills defaults
    Value.is_lambda()         the three shapes nFunction covers
    Value.is_primop()
    Value.is_primop_app()
    Value.lambda_name()       what it was bound to, or ""
    Value.lambda_arg()        the name bound to the WHOLE argument
    Value.has_formals()
    Value.accepts_extra()     the ellipsis
    Value.formal_names()      alphabetical
    Value.defaulted_formals() a subset of the above
    Value.primop_name()
    Value.primop_arity()      the one honest arity
    Value.primop_args()
    Value.doc()               getDoc, and it BLOCKS

    huggorm.signature_of(v) -> inspect.Signature

The plan above was written against the MOCK, which `tasks/060`
deleted, so its first bullet ("the mock needs a function kind") was
already dead: real Nix has real lambdas and `eval_expr` produces
them. Everything else in that section held.

Carl pre-approved `Cxx()` bodies in declarations for this, with
documented justifications. No new C++ in `cpp/eval.hpp`, and every
body carries the why-a-declaration-cannot-say-this comment the
approval asked for.

### One arm, three payloads

`@guard("function")` is NECESSARY AND NOT SUFFICIENT, and that is the
shape of the whole change. The guard is generated from `@tagged`,
which names the twelve `nix::ValueType` arms - and the three function
shapes are not types, they are storage tags under one type
(value.hh:1111). So the guard proves `nFunction` and says nothing
about which of the three, and reading `lambda()` off a builtin is
exactly the payload reinterpretation the `Value` docstring exists to
warn about.

Every body therefore checks its own shape first. `tests/
test_function.py::test_a_builtin_read_as_a_lambda_is_refused` holds
all twelve of those checks, in both directions.

**Not perturbed, and deliberately.** Removing a sub-guard produces
undefined behaviour rather than a wrong answer, so the failure is a
crash or a garbage number that takes the suite with it - not one
clean assertion. Stated here instead of run.

### Three things measured after writing the opposite

Each of these was in a docstring before it was in a gate, and each
gate refuted the docstring.

**The result of `apply` is in WHNF, not a thunk.**

    assert 'int' == 'thunk'

`callFunction` evaluates the lambda's body with `Expr::eval`
(eval.cc:1600), which produces an evaluated value. The laziness is
one level down: `x: { a = x + 1; }` answers a forced attribute set
whose `a` is still a thunk, which is now the second half of that
gate.

**Formals arrive in interning order.**

    assert ['zebra', 'apple', 'mango'] == ['apple', 'mango', 'zebra']

`validateFormals` sorts them by `std::tie(a.name, a.pos)`
(parser-state.hh:302) - and `name` is a `Symbol`, an interning ID. So
upstream's order is the order each name was first seen ANYWHERE in
the process: neither source order nor alphabetical, and not
reproducible between two runs.

This is the same fact this repo already records for attribute sets,
where `Bridge::sorted()` pays a sort to hide it. The bodies sort by
name for the same reason. Source order would be the other defensible
answer and is not available - `Formal::pos` survives, but sorting by
it needs positions the declaration does not carry.

Broken on purpose, with the sort removed from both bodies:

    FAILED test_formals_come_back_alphabetically
    1 failed, 359 passed, 10 deselected

**`doc()` on a lambda reads the source file, and throws if it is
gone.**

    IndexError: basic_string::substr: __pos (which is 3) >
                this->size() (which is 0)

A doc comment is NOT stored. `getDoc` calls `getInnerText`, which
calls `getSnippetUpTo`, which calls `Pos::getSource`, which calls
`path.readFile()` (position.cc:49). So an accessor that looks like a
field read opens a file, and it is marked `@blocks` - `tasks/067`'s
class of surprise.

And when the file has moved, `getSource` catches its own error and
answers "", after which `getInnerText` does
`substr(3, size() - 3 - 2)` on that empty string and throws from
inside libexpr (nixexpr.cc:644-648). Upstream's bug.

The binding catches that ONE exception and reports the cause. "" was
the first answer and is wrong for this repo's own reason: it would say
"no documentation" where the truth is "cannot read the
documentation", and an absence standing in for a failure is the named
failure mode.

### The refusal that matters

`apply_auto` refuses a function that declares no formals, and that is
why it is a separate method rather than a convenience on `apply`.

`autoCallFunction` answers `res = fun` in that case (eval.cc:1795-
1798) - the function itself, UNAPPLIED, with nothing to say that the
arguments went nowhere. A caller could not tell that from a call that
legitimately returned a function. That is this repo's named failure
mode sitting in libexpr, and goal 1 says a binding may not be more
permissive than the C++ it binds.

Broken on purpose:

    FAILED test_a_formal_less_lambda_is_refused_by_name
    Failed: DID NOT RAISE Exception
    1 failed, 359 passed, 10 deselected

Two more things `autoCallFunction` does that the docstring now
states. It FORCES the function (eval.cc:1783), unlike everything else
here. And it follows a `__functor` attribute (eval.cc:1786-1792), so
Nix considers some attribute sets callable - which `@guard("function")`
refuses, and a caller reaches by applying the `__functor` attribute
itself.

### No arity for a lambda

`x: y: body` is a function returning a function, so nothing can say
how many arguments it takes without applying it. `getDoc` leaves a
lambda's `arity` at 0 with upstream's own FIXME beside it
(eval.cc:622). No accessor here offers a lambda one, because any that
did would be lying.

Only a builtin declares an arity, and `primop_arity` reads it off
`PrimOp` rather than through `getDoc` - which matters, because
`getDoc`'s whole primop branch sits behind `if (primOp.doc)`
(eval.cc:578). A builtin with no documentation has an arity that
`getDoc` will not report.

The third shape has no arity at all: `getDoc` has no branch for a
`primOpApp`, and the applied arguments sit in a chain nothing walks.
`primop_arity` refuses it and `signature_of` raises rather than
answering a catch-all.

### The two duals, composed

`test_python_calls_nix_calling_python` is the gate neither `tasks/033`
nor this task had.

They are duals with OPPOSITE threading, which this file already said:
a primop written in Python is SYNC because it runs inside evaluation
and cannot await, and applying a function BLOCKS so the binding
releases the GIL around it. The test crosses both in one call - Python
applies a function, the evaluator runs with the GIL released, and then
calls back into Python and reacquires it.

That reacquire inside a release is the deadlock the composition would
produce if either half were wrong, and nothing else in the suite
reaches it: 033 never applies a function from Python, and the rest of
this file never registers one.

### `inspect.Signature`, and what Python requires that Nix does not

`huggorm.signature_of(value)`, hand-written in `huggorm/signature.py`
for the reason the module says: the binding answers FACTS, one
declared accessor each, and assembling them into a library type is a
mapping no declaration describes.

    { a, b ? 2 }:        (*, a, b=Ellipsis)
    { a, ... }:          (*, a, **kwargs)
    x:                   (x, /)
    builtins.add         (e1, e2, /)

`Ellipsis` is the default for a formal that has one, because a Nix
default is an unevaluated expression in the lambda's own environment
and cannot cross. What a caller needs is that the argument is
OPTIONAL. `None` would claim a default Nix does not have, and
`Parameter.empty` would say the parameter is required.

Two things Python requires and Nix does not, both found by writing the
gate:

- a parameter with no default may not follow one, even among
  keyword-only parameters, so the required formals are ordered first;
- Nix names are wider than Python's. A dash is legal in a Nix
  identifier and `class` is an ordinary Nix name, and `Parameter`
  raises on either - so an unusable name falls back to a positional
  one rather than turning an unusual function into a failure to
  introspect it at all. `formal_names` still answers the real name.

A quoted formal is NOT one of those cases, which the gate found the
hard way: `{ "with a space" ? 1 }` is a syntax error in Nix, unlike
the same spelling in an attribute set.

Nothing is attached as `__signature__` and nothing is cached, which is
what this file asked for: a proxy is built for every function value
that crosses, and paying for introspection nobody asked for would make
realizing a tree of functions quadratic.

### What is left

**`__call__`, so `await f(x)` works.** The ask says "call cleanly" and
this delivers `await f.apply(x)`. A dunder cannot be declared today -
the same family of gap as `tasks/076`'s `@property` and `tasks/084`'s
virtuals - and the declaration is where it has to be solved, because
`__call__` on the async wrapper alone would leave the RPC client and
the sync binding without it. Not opened as its own task: it is one
more instance of "the DSL cannot say this", and 076 is where that
question already lives.

**A cross-state argument is unchecked**, and it is not new. `apply(f,
arg)` hands one state's GC value to another state's evaluator if a
caller mixes them, and so does `EvalState.force` and
`EvalState.attrs_set` today - nothing compares `core()`. Named here
rather than widened or silently fixed, because the fix belongs to
whichever task decides what a state's ownership of a value means.

**No remote gate.** `Value` to `Value` over the wire is already proven
by `force` and `attrs_set`, so an `apply` rpc adds confirmation rather
than machinery, and the schema needs nothing new. Worth adding when
something remote actually calls a function.
