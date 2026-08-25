# A value can be a function

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
