"""A value that is a function: applying one, and reading its shape.

`nFunction` is ONE type name over THREE payloads - a lambda, a
builtin, and a builtin holding some of its arguments - so most of
these gates are about the difference between them. `@guard("function")`
proves the arm and nothing more; the sub-checks in each body are what
keep a `lambda()` read off a builtin from reinterpreting the payload,
and several tests here exist only to prove those sub-checks refuse.

Two things this file is deliberately not testing.

Arity for a lambda, because there is none. `x: y: body` is a function
returning a function, and nothing can say how many arguments it wants
without applying it - `getDoc` leaves a lambda's `arity` at 0 with
upstream's own FIXME beside it. Only a builtin declares one.

A sub-guard by PERTURBATION. Removing one produces undefined
behaviour - reading a lambda payload off a builtin - so the failure is
a crash or a wrong number rather than one clean assertion, and it
takes the suite with it. `tasks/034` says so rather than this file
running it.
"""

from typing import Any

import pytest

URI = "dummy://"


@pytest.fixture
def state() -> Any:
    from huggorm_bindings import EvalState

    return EvalState(URI)


def fn(state: Any, expr: str) -> Any:
    """A forced function value from an expression."""
    v = state.eval_expr(expr)
    assert v.type_name() == "function", v.type_name()
    return v


# -- applying --------------------------------------------------------------

def test_one_argument_applies(state: Any) -> None:
    """`f x`, which is how every Nix function is called."""
    f = fn(state, "x: x + 1")
    got = f.apply(state.make_int(41))
    state.force(got)
    assert got.integer() == 42


def test_a_function_returns_a_function(state: Any) -> None:
    """CURRYING, which is why there is no arity.

    `x: y: x + y` applied once is still a function, and the type name
    says so - this is the shape any accessor offering a single arity
    for a lambda would be lying about."""
    f = fn(state, "x: y: x + y")
    once = f.apply(state.make_int(40))
    state.force(once)
    assert once.type_name() == "function", "one argument left"

    twice = once.apply(state.make_int(2))
    state.force(twice)
    assert twice.integer() == 42


def test_the_result_comes_back_in_whnf(state: Any) -> None:
    """The top level is forced and everything inside stays lazy.

    Written the other way first - "the result is a thunk" - and this
    gate refuted it with `assert 'int' == 'thunk'`. `callFunction`
    evaluates the lambda's body with `Expr::eval`, which produces an
    evaluated value.

    So the laziness is one level down, which is what the second half
    asserts: an attribute set comes back forced, and its attributes
    do not."""
    got = fn(state, "x: x + 1").apply(state.make_int(41))
    assert got.type_name() == "int", "already evaluated"
    assert got.integer() == 42

    nested = fn(state, "x: { a = x + 1; }").apply(state.make_int(41))
    assert nested.type_name() == "attrs", "the set itself is forced"
    inner = nested.get("a")
    assert inner.type_name() == "thunk", "and its contents are not"
    state.force(inner)
    assert inner.integer() == 42


def test_applying_a_non_function_is_refused(state: Any) -> None:
    """The guard, and the reason every accessor here has one.

    `nix::Value` is a tagged union whose readers are `noexcept` and
    undefined on the wrong tag, so this has to fail as an error rather
    than as a reinterpretation."""
    with pytest.raises(Exception, match="not function"):
        state.eval_expr("42").apply(state.make_int(1))


# -- the three shapes ------------------------------------------------------

def test_a_lambda_and_a_builtin_are_both_functions(state: Any) -> None:
    """One type name, told apart by the shape accessors.

    `type_name` cannot separate them - that is the whole reason
    `is_lambda` and `is_primop` exist - so this asserts the pair
    agrees for each."""
    lam = fn(state, "x: x")
    assert lam.is_lambda()
    assert not lam.is_primop()
    assert not lam.is_primop_app()

    builtin = fn(state, "builtins.add")
    assert builtin.is_primop()
    assert not builtin.is_lambda()
    assert not builtin.is_primop_app()


def test_a_partly_applied_builtin_is_the_third_shape(state: Any) -> None:
    """`builtins.add 1` is a builtin holding one argument.

    The shape with no introspection at all: `getDoc` has no branch for
    it, and nothing in libexpr says how many arguments are still
    wanted. So this asserts what it IS and that the introspection
    refuses it, which together are the whole of what can be said."""
    partial = fn(state, "builtins.add 1")
    assert partial.is_primop_app()
    assert not partial.is_primop(), "not a bare builtin any more"
    assert not partial.is_lambda()

    with pytest.raises(Exception, match="not a builtin"):
        partial.primop_arity()
    assert partial.doc() == "", "no getDoc branch for this shape"

    # It still applies, which is the point of the shape existing.
    got = partial.apply(state.make_int(41))
    state.force(got)
    assert got.integer() == 42


def test_a_builtin_read_as_a_lambda_is_refused(state: Any) -> None:
    """The SUB-GUARD, which is the one this whole file is built around.

    `@guard("function")` passes: a builtin is `nFunction`. Reading
    `lambda()` off it would then reinterpret the payload, which is
    undefined rather than wrong. So every `lambda_*` body checks
    `isLambda()` first, and this is that check failing on purpose.

    Both directions, because a guard that only refuses one way is
    half a guard."""
    builtin = fn(state, "builtins.add")
    for read in ("lambda_name", "lambda_arg", "has_formals",
                 "accepts_extra", "formal_names", "defaulted_formals"):
        with pytest.raises(Exception, match="not a lambda"):
            getattr(builtin, read)()

    lam = fn(state, "x: x")
    for read in ("primop_name", "primop_arity", "primop_args"):
        with pytest.raises(Exception, match="not a builtin"):
            getattr(lam, read)()


# -- reading a lambda ------------------------------------------------------

def test_a_simple_lambda_names_its_argument(state: Any) -> None:
    """`x: body` binds the whole argument and declares no formals."""
    f = fn(state, "x: x")
    assert f.lambda_arg() == "x"
    assert not f.has_formals()
    assert f.formal_names() == []
    assert not f.accepts_extra()


def test_formals_come_back_alphabetically(state: Any) -> None:
    """ALPHABETICAL, and the sort is ours rather than upstream's.

    This gate is why the sort exists. Upstream's `validateFormals`
    sorts by `std::tie(a.name, a.pos)` where `name` is a Symbol - an
    interning ID - so its order is the order each name was first seen
    ANYWHERE in the process. Written expecting upstream's order to be
    usable, this failed with

        assert ['zebra', 'apple', 'mango'] == ['apple', 'mango', 'zebra']

    which is exactly the interning order of a test that had just
    mentioned zebra first. Not reproducible between runs, so the
    binding sorts.

    The names below are declared in a deliberately wrong order, and
    `zebra` is first so that interning order and alphabetical order
    disagree."""
    f = fn(state, "{ zebra, apple, mango }: apple")
    assert f.has_formals()
    assert f.formal_names() == ["apple", "mango", "zebra"]
    assert f.lambda_arg() == "", "no name for the set itself"


def test_a_default_is_reported_as_a_fact_not_a_value(state: Any) -> None:
    """A subset of the names, because a default cannot cross.

    A default is an unevaluated expression in the lambda's own
    environment. `inspect.Parameter` needs to know one EXISTS rather
    than what it is, which is the whole use for this."""
    f = fn(state, "{ a, b ? 2, c ? 3 }: a")
    assert f.formal_names() == ["a", "b", "c"]
    assert f.defaulted_formals() == ["b", "c"]


def test_the_ellipsis_is_visible(state: Any) -> None:
    """`...` changes what `apply_auto` PASSES, not just what it takes.

    With an ellipsis upstream forwards every argument it was given;
    without one it forwards only the declared formals. So this is not
    cosmetic and a caller has to be able to read it."""
    assert fn(state, "{ a, ... }: a").accepts_extra()
    assert not fn(state, "{ a }: a").accepts_extra()


def test_a_set_can_be_named_alongside_its_formals(state: Any) -> None:
    """`{ a, b } @ rest:` declares formals AND binds the whole set.

    Both accessors answer, which is why `lambda_arg` is documented as
    "the name bound to the whole argument" rather than "the parameter
    name"."""
    f = fn(state, "{ a, b } @ rest: rest")
    assert f.lambda_arg() == "rest"
    assert f.formal_names() == ["a", "b"]


def test_a_lambda_carries_the_name_it_was_bound_to(state: Any) -> None:
    """For a human, and empty when there is nothing to report."""
    named = state.eval_expr("(let f = x: x; in f)")
    state.force(named)
    assert named.lambda_name() == "f"
    assert fn(state, "x: x").lambda_name() == ""


# -- reading a builtin -----------------------------------------------------

def test_a_builtin_declares_an_arity_and_its_arguments(state: Any) -> None:
    """The one place an arity is honest.

    Read off `PrimOp` rather than through `getDoc`, and the difference
    is measurable: `getDoc`'s whole primop branch sits behind `if
    (primOp.doc)`, so a builtin with no documentation has an arity
    that `getDoc` will not report."""
    add = fn(state, "builtins.add")
    assert add.primop_name() == "add"
    assert add.primop_arity() == 2
    assert add.primop_args() == ["e1", "e2"]


def test_a_builtin_has_documentation(state: Any) -> None:
    """`getDoc`'s primop branch, which is a real doc string."""
    doc = fn(state, "builtins.add").doc()
    assert doc, "builtins.add is documented upstream"
    assert "return" in doc.lower() or "sum" in doc.lower(), doc


def test_a_lambda_doc_is_prose_and_reads_the_source(
        state: Any, tmp_path: Any) -> None:
    """`doc()` answers a DIFFERENT shape per kind, and says so.

    A lambda gets prose built for the REPL - "Function `name` defined
    at ..." - rather than a doc string, because that is what
    `getDoc` builds for one.

    And it reads the FILE. A doc comment is not stored: getDoc calls
    `getInnerText`, which calls `getSnippetUpTo`, which calls
    `Pos::getSource`, which calls `path.readFile()`. That is why
    `doc()` is marked as blocking, and this is the gate that shows the
    I/O is real - the comment only appears in the answer if the file
    was opened."""
    src = tmp_path / "documented.nix"
    src.write_text("/** Adds one to its argument. */\nx: x + 1\n")
    f = state.eval_file(str(src))
    state.force(f)

    doc = f.doc()
    assert "Function" in doc, doc
    assert "Adds one to its argument." in doc, "the source was not read"


def test_a_lambda_doc_reports_a_source_that_went_away(
        state: Any, tmp_path: Any) -> None:
    """A missing source RAISES, with a message about the source.

    Written expecting "" - `getSource` catches its own read error and
    answers nothing - and it failed with

        IndexError: basic_string::substr: __pos (which is 3) >
                    this->size() (which is 0)

    because `getInnerText` then does `substr(3, size() - 3 - 2)` on
    that empty string. So a doc comment whose file has moved throws
    from inside libexpr, and this is upstream's bug rather than a
    choice.

    The binding catches that one exception and reports the cause. ""
    would say "no documentation" where the truth is "cannot read the
    documentation", and an absence standing in for a failure is this
    repo's named failure mode."""
    src = tmp_path / "vanishing.nix"
    src.write_text("/** Gone soon. */\nx: x\n")
    f = state.eval_file(str(src))
    state.force(f)
    src.unlink()

    with pytest.raises(Exception, match="no longer readable"):
        f.doc()


# -- by name ---------------------------------------------------------------

def test_an_attribute_set_applies_by_name(state: Any) -> None:
    """`autoCallFunction`: one round trip for a whole argument set."""
    f = fn(state, "{ a, b }: a + b")
    args = state.make_attrs()
    state.attrs_set(args, "a", state.make_int(40))
    state.attrs_set(args, "b", state.make_int(2))

    got = f.apply_auto(args)
    state.force(got)
    assert got.integer() == 42


def test_a_missing_argument_falls_back_to_its_default(state: Any) -> None:
    """What `apply_auto` adds over applying the set with `apply`.

    `apply` would hand the set straight to the lambda and the missing
    formal would be an error. This fills it from the formal's own
    default, which is what `--arg` does."""
    f = fn(state, "{ a, b ? 2 }: a + b")
    args = state.make_attrs()
    state.attrs_set(args, "a", state.make_int(40))

    got = f.apply_auto(args)
    state.force(got)
    assert got.integer() == 42


def test_a_formal_less_lambda_is_refused_by_name(state: Any) -> None:
    """The refusal that is the reason `apply_auto` exists separately.

    Upstream answers `res = fun` for a function with no formals - the
    function itself, UNAPPLIED, with nothing to say that the arguments
    went nowhere. A caller could not tell that from a call that
    returned a function legitimately, which is this repo's named
    failure mode sitting in libexpr.

    So the binding refuses instead: a binding may not be more
    permissive than the C++ it binds."""
    f = fn(state, "x: x")
    args = state.make_attrs()
    state.attrs_set(args, "a", state.make_int(1))

    with pytest.raises(Exception, match="declares formals"):
        f.apply_auto(args)


def test_by_name_needs_an_attribute_set(state: Any) -> None:
    """The guard covers `self` and not the ARGUMENT.

    `@guard` is generated for the receiver, so a body taking another
    Value has to check it itself - and `attrs()` off a list is the
    same payload reinterpretation one argument along."""
    f = fn(state, "{ a }: a")
    with pytest.raises(Exception, match="attribute set"):
        f.apply_auto(state.make_int(1))


def test_a_required_argument_with_no_value_raises_nix(state: Any) -> None:
    """A declared Nix error, not a binding error.

    MissingArgumentError comes from the evaluator, so it crosses the
    typed-error path rather than arriving as a RuntimeError this
    layer invented."""
    from huggorm_bindings.errors import NixError

    f = fn(state, "{ a, b }: a + b")
    args = state.make_attrs()
    state.attrs_set(args, "a", state.make_int(40))

    with pytest.raises(NixError, match="without a value"):
        got = f.apply_auto(args)
        state.force(got)


# -- the two duals, composed -----------------------------------------------

def test_python_calls_nix_calling_python(state: Any) -> None:
    """The collision point of `tasks/033` and `tasks/034`.

    Those two are duals with OPPOSITE threading. A primop implemented
    in Python is SYNC - it runs inside evaluation and cannot await -
    and applying a function BLOCKS, so the binding releases the GIL
    around it. This test crosses both in one call: Python applies a
    function, the evaluator runs with the GIL released, and then calls
    back into Python and reacquires it.

    That reacquire inside a release is the deadlock this composition
    would produce if either half were wrong, and neither task's own
    gates cross it - 033 never applies a function from Python, and the
    rest of this file never registers one."""
    calls: list[int] = []

    def double(v: Any) -> Any:
        calls.append(v.integer())
        return state.make_int(v.integer() * 2)

    state.register_primop("dbl", 1, double)

    # The primop AS A VALUE, applied from Python rather than from an
    # expression - so the whole path is Python -> nix -> Python.
    f = fn(state, "builtins.dbl")
    assert f.is_primop()
    assert f.primop_arity() == 1

    got = f.apply(state.make_int(21))
    state.force(got)
    assert got.integer() == 42
    assert calls == [21], "the callback ran inside the applied call"


def test_a_python_primop_applied_through_a_lambda(state: Any) -> None:
    """The same crossing with a Nix lambda in the middle.

    `x: builtins.trip x` is a lambda, so applying it enters the
    evaluator, which applies a primop, which calls Python. One more
    frame than the test above, and the frame is Nix's."""
    state.register_primop("trip", 1, lambda v: state.make_int(v.integer() * 3))

    f = fn(state, "x: builtins.trip x")
    got = f.apply(state.make_int(14))
    state.force(got)
    assert got.integer() == 42


# -- as inspect.Signature --------------------------------------------------
#
# The other half of Carl's ask: "which types are most appropriate to
# be able to introspect in Python to be able to call cleanly". A
# `Signature` is the type Python's own tooling already reads, so these
# assert against `str(sig)` where it is legible - that string is what
# `help()` shows a caller.


async def asig(expr: str) -> Any:
    """The signature of a function from an expression, async surface."""
    from huggorm.signature import signature_of
    from huggorm_generated import AsyncEvalState

    state = AsyncEvalState(URI)
    try:
        v = await state.eval_expr(expr)
        await state.force(v)
        return await signature_of(v)
    finally:
        await state.aclose()


async def test_formals_become_keyword_only() -> None:
    """By NAME, because that is how `apply_auto` passes them.

    A formal is filled from an attribute set by name, so KEYWORD_ONLY
    is the parameter kind that describes it - and `str()` of the
    signature is what `help()` shows."""
    assert str(await asig("{ a, b }: a + b")) == "(*, a, b)"


async def test_a_default_shows_as_ellipsis() -> None:
    """A default EXISTS, and its value cannot cross.

    A Nix default is an unevaluated expression in the lambda's own
    environment. `Ellipsis` says "optional, and I am not claiming to
    know what it defaults to", where None would claim a default Nix
    does not have."""
    assert str(await asig("{ a, b ? 2 }: a")) == "(*, a, b=Ellipsis)"


async def test_a_required_formal_comes_before_an_optional_one() -> None:
    """Python's rule, not Nix's, and it has to be obeyed.

    A parameter with no default may not follow one, even among
    keyword-only parameters - `Signature` raises. Nix has no such
    rule and the binding answers alphabetically, so this reorders:
    required alphabetically, then optional alphabetically.

    `z` before `a` is the case that fails without it."""
    assert str(await asig("{ a ? 1, z }: z")) == "(*, z, a=Ellipsis)"


async def test_an_ellipsis_becomes_var_keyword() -> None:
    """`...` accepts names the lambda does not mention, which is
    exactly what **kwargs means."""
    assert str(await asig("{ a, ... }: a")) == "(*, a, **kwargs)"


async def test_a_simple_lambda_is_positional() -> None:
    """`x: body` takes a value, not a keyword.

    POSITIONAL_ONLY because `apply` passes one value and the name is
    the lambda's own binding - no caller can use it as a keyword. It
    is reported so `help()` can show what the author called it."""
    assert str(await asig("x: x + 1")) == "(x, /)"


async def test_a_builtin_is_positional_with_its_declared_names() -> None:
    """The one place an arity is honest, so the one place a signature
    is complete."""
    assert str(await asig("builtins.add")) == "(e1, e2, /)"


async def test_a_partly_applied_builtin_has_no_signature() -> None:
    """It refuses rather than guessing.

    `builtins.add 1` still wants an argument and nothing says how
    many: the applied arguments are in a chain nothing walks, and
    `getDoc` has no branch for the shape. `(*args)` would be a guess
    dressed as an answer."""
    with pytest.raises(TypeError, match="partially-applied"):
        await asig("builtins.add 1")


async def test_a_non_function_has_no_signature() -> None:
    """And the message names what it got."""
    with pytest.raises(TypeError, match="int has no signature"):
        await asig("42")


async def test_an_unusable_formal_name_does_not_lose_the_signature() -> None:
    """Nix identifiers are wider than Python's, in two ways.

    A Nix name may contain `-` and `'`, so `foo-bar` is a legal
    formal and not a Python identifier. And `class` is a Python
    keyword and an ordinary Nix name. `inspect.Parameter` raises on
    either, which would turn an unusual function into a failure to
    introspect it at all - so an unusable name falls back and the
    signature survives.

    `formal_names` still answers the real names, because that is the
    accessor whose job is facts; this one builds a Python object and
    Python's rules apply to it.

    Written first with a QUOTED formal, which Nix rejects outright -
    a quoted name is legal in an attribute set and a syntax error in
    formals. So the fallback defends against the names Nix does
    allow, not the wider set an attrset has."""
    sig = await asig("{ foo-bar, class, ok }: ok")
    assert "foo-bar" not in str(sig), "not a Python identifier"
    assert "class" not in str(sig), "a Python keyword"
    assert "ok" in str(sig)
    assert len(sig.parameters) == 3, "all three survive, two renamed"


async def test_the_signature_says_how_to_call_it() -> None:
    """The point of choosing `Signature`: it BINDS.

    `Signature.bind` is Python's own argument matcher, so a caller can
    check a call before making it - and the names it validates against
    are the ones `apply_auto` will actually fill."""
    sig = await asig("{ a, b ? 2 }: a + b")
    bound = sig.bind(a=40)
    assert bound.arguments == {"a": 40}
    with pytest.raises(TypeError):
        sig.bind(a=1, nope=2)
