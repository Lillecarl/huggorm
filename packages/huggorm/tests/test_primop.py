"""A primop implemented in Python, which is C++ calling back.

Everything else in this suite is Python calling the evaluator. These
run the other way: the evaluator calls a Python function in the middle
of an evaluation, on its own thread, with the GIL reacquired.

In-process only, and that is a decision rather than a gap. A remote
registration would make the evaluator call back over the socket once
per invocation on its evaluation thread, so `ir.NOT_DATA`
keeps it from getting an rpc - `test_no_rpc_surface` below is what
holds that.
"""

import gc
from typing import Any

import pytest
from nixversion import ERROR_PREFIX, STRING_ORIGIN

URI = "dummy://"


@pytest.fixture
def state() -> Any:
    from huggorm_bindings import EvalState, Store

    return EvalState(Store(URI))


def test_a_python_function_answers_as_a_builtin(state: Any) -> None:
    """The whole point, in four lines.

    `builtins.add2` is Python. The evaluator forces both arguments,
    reacquires the GIL, calls the function, and takes the Value it
    returns as the result of the primop."""
    state.register_primop(
        "add2", 2, lambda a, b: state.make_int(a.integer() + b.integer()))

    got = state.eval_expr("builtins.add2 40 2")
    assert got.integer() == 42


def test_a_primop_can_answer_null(state: Any) -> None:
    """`make_null` is the one producer with no argument."""
    state.register_primop("nothing", 1, lambda _v: state.make_null())

    assert state.eval_expr("builtins.nothing 1 == null").boolean() is True
    assert state.make_null().type_name() == "null"


def test_the_arguments_arrive_forced(state: Any) -> None:
    """A primop receives THUNKS, and this one never sees one.

    `40 + 2` is unevaluated where the primop is applied, so a binding
    that did not force would hand Python a value whose `type_name` is
    "thunk" and whose `integer` raises. The assertion is inside the
    callback, which is the only place that can see it."""
    seen: list[str] = []

    def peek(v: Any) -> Any:
        seen.append(v.type_name())
        return state.make_int(v.integer() * 2)

    state.register_primop("twice", 1, peek)
    assert state.eval_expr("builtins.twice (40 + 2)").integer() == 84
    assert seen == ["int"], "an unforced argument reached Python"


def test_a_python_exception_becomes_a_nix_error(state: Any) -> None:
    """A failure inside the callback is in the middle of a C++
    evaluation, and it has to leave as a Nix error rather than unwind
    through those frames.

    The message carries the Python one, so a caller can tell WHICH
    call failed rather than only that something did."""
    def boom(v: Any) -> Any:
        raise ValueError("the primop said no")

    state.register_primop("boom", 1, boom)

    with pytest.raises(Exception) as caught:
        state.eval_expr("builtins.boom 1")
    assert "the primop said no" in str(caught.value)


def test_a_failing_primop_names_the_class_and_not_the_traceback(
        state: Any) -> None:
    def boom(v: Any) -> Any:
        raise ValueError("the primop said no")

    state.register_primop("boom2", 1, boom)

    with pytest.raises(Exception) as caught:
        state.eval_expr("builtins.boom2 1")
    assert "ValueError: the primop said no" in str(caught.value)
    assert "Traceback" not in str(caught.value)


def test_a_primop_raising_a_nix_error_shows_it_bare(state: Any) -> None:
    """The way a Python primop rejects its input, as a C++ one does:
    one of huggorm's own errors, whose message Nix shows as it is."""
    from huggorm_bindings.errors import EvalError

    def reject(v: Any) -> Any:
        raise EvalError("no such user")

    state.register_primop("reject", 1, reject)

    with pytest.raises(EvalError) as caught:
        state.eval_expr("builtins.reject 1")
    # The last line, with no class name on it and no position under it:
    # the trace line above already names the call site, once.
    text = str(caught.value)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    assert lines[-1] == f"{ERROR_PREFIX}no such user", lines
    assert text.count(f"{STRING_ORIGIN}:1:1") == 1, text


def test_returning_something_that_is_not_a_value_is_an_error(
        state: Any) -> None:
    """The failure a too-narrow catch would have let escape.

    A Python function returning a plain `int` fails in `nb::cast`,
    which raises `cast_error` and NOT `python_error`. Catching only
    the latter would let it unwind through C++ evaluation frames."""
    state.register_primop("wrong", 1, lambda v: 42)

    with pytest.raises(Exception) as caught:
        state.eval_expr("builtins.wrong 1")
    assert "did not return a Value" in str(caught.value)


def test_zero_arity_is_a_lazy_constant(state: Any) -> None:
    """Arity 0 is what Nix makes it: a name bound to a value that is
    computed when an evaluation first reads it.

    The callable takes no arguments. It does not run at registration,
    nor for an evaluation that does not read the name."""
    calls: list[int] = []

    def answer() -> Any:
        calls.append(1)
        return state.make_int(42)

    state.register_primop("answer", 0, answer)
    assert state.eval_expr("1 + 1").integer() == 2
    assert calls == [], "the constant ran before anything read it"
    assert state.eval_expr("builtins.answer + 1").integer() == 43
    assert calls == [1]


def test_a_negative_arity_is_refused(state: Any) -> None:
    with pytest.raises(ValueError, match="negative"):
        state.register_primop("nothing", -1, lambda: None)


def test_an_empty_name_is_refused(state: Any) -> None:
    with pytest.raises(ValueError):
        state.register_primop("", 1, lambda v: v)


def test_a_primop_belongs_to_one_state(state: Any) -> None:
    """Registration is per-EvalState, not global.

    `RegisterPrimOp` is the global route and is consumed once, during
    construction; this uses `addPrimOp` on a live state. So a second
    evaluator does not see it, which is what makes the call safe to
    offer at all - a global would let one caller change every
    evaluator in the process."""
    from huggorm_bindings import EvalState, Store

    state.register_primop("mine", 1, lambda v: v)
    assert state.eval_expr("builtins.mine 1").integer() == 1

    other = EvalState(Store(URI))
    with pytest.raises(Exception) as caught:
        other.eval_expr("builtins.mine 1")
    assert "mine" in str(caught.value)


def test_filling_the_base_environment_raises_rather_than_corrupts(
        state: Any) -> None:
    """The gate the carried patch exists for.

    Stock nix allocates the base environment at BASE_ENV_SIZE = 128
    and `addPrimOp` tests NO bound before
    `baseEnv.values[baseEnvDispl++] = v`. Nix publishes 119 names, so
    the tenth registration writes past the end of a GC allocation -
    and the collector HIDES it, because Boehm rounds an allocation up
    to a size class and the write lands in the block's slack.

    `nix/patches/nix-base-env-size.patch` raises the size to 512 and
    makes the write test the bound. This registers past 512 and reads
    an error.

    Without the patch this test does not fail - it CORRUPTS, silently,
    which is why the patch had to come first and why this could not be
    written before it."""
    made = 0
    with pytest.raises(Exception) as caught:
        for i in range(600):
            state.register_primop(f"filler{i}", 1, lambda v: v)
            made += 1

    assert "base environment is full" in str(caught.value)
    # Past the stock size, so the patch is what is being read here and
    # not some other limit.
    assert made > 128, made


async def test_a_remote_state_refuses_a_primop(client: Any) -> None:
    """A callable does not cross a wire, by decision.

    The generator gives a parameter spelled `nb::object` no wire
    number, so a remote EvalState refuses `register_primop` and says
    why. An accidental rpc would make the evaluator call back over the
    socket once per invocation, on its evaluation thread."""
    state = await client.acquire("EvalState", await client.acquire("Store", "dummy://"))
    with pytest.raises(TypeError, match="runs only in process: parameter 'fn'"):
        await state.register_primop("f", 1, lambda x: x)
    await state.aclose()


def test_every_registered_name_is_findable(state: Any) -> None:
    """Registration must SORT, and nothing else here says so.

    `addPrimOp` appends to the `builtins` set and to the static base
    environment and sorts neither. Upstream gets away with it because
    `createBaseEnv` sorts once after adding them all, and says why
    (`primops.cc:5449`): "attribute lookups expect it to be sorted".
    Registering on a LIVE state runs after that sort.

    So an unsorted append leaves a name that IS there and cannot be
    found - the lookup binary-searches past it while the suggestion
    engine, which scans, still sees it:

        error: attribute 'wrong' missing
               Did you mean wrong?

    WHICH names trip it is not fully explained, and this says so
    rather than pretending. `Symbol` orders by interning id
    (`symbol-table.hh:73`, a defaulted `<=>`), so a freshly interned
    name takes the highest id and an append usually KEEPS the order -
    which is why eight arbitrary names all pass without the sorts and
    `wrong` does not. The mechanism is upstream's documented
    requirement; the trigger is name-dependent in a way not chased
    down here.

    So the list carries the name MEASURED to fail. A tidy-up that
    swapped it for a nicer one would silently retire the only case
    known to discriminate."""
    names = ["wrong", "zzz", "aaa", "mmm", "abs2", "toStr", "qqq"]
    for n in names:
        state.register_primop(n, 1, lambda v: v)

    for n in names:
        assert state.eval_expr(f"builtins.{n} 7").integer() == 7, n


# ---- the cycle a stored callable makes -----------------------------
#
# huggorm#93. A primop's callable normally closes over the state that
# registered it, because the result comes from `state.make_int`. The
# state then reaches the callable through nix's base env, and the
# callable reaches the state through its closure cell - a cycle whose
# first arm lives in C++ memory Python's collector cannot walk.
#
# Measured before the fix: two `gc.collect()` calls did not free it,
# and the suite reported four leaked EvalState instances at shutdown.
#
# A CANARY rather than a weakref, and not by preference: the bound
# type has no weakref slot ("cannot create weak reference to
# 'huggorm_bindings.eval.EvalState'"). The canary sits in the closure,
# so it dies exactly when the closure does.


class _Canary:
    """Records its own death, which is the whole measurement."""

    def __init__(self, died: list[str]) -> None:
        self._died = died

    def __del__(self) -> None:
        self._died.append("collected")


def test_a_primop_closing_over_its_state_does_not_leak_it() -> None:
    """The state must be collectable, and it holds the callable.

    Nothing here calls `del` on `state`. That looks like a neutral way
    to drop a reference and is not: the lambda closes over the same
    binding through a cell, so deleting it EMPTIES the cell and breaks
    the cycle by hand. A probe that did it read "no leak" as "no
    cycle" and was wrong (huggorm#93).

    So the reference goes out of scope on its own, and the collector
    is asked.

    Perturbation: make `evaluator_tp_traverse` report no callables.
    This fails, and the four leaked instances come back.

    Making `evaluator_tp_clear` release nothing changes NOTHING, and
    that was measured rather than assumed. CPython needs `tp_traverse`
    on every type in a cycle and `tp_clear` on only ONE of them; the
    other participants here are a function object and a cell, and both
    carry their own. So traverse is the load-bearing half and this
    gate drives only that."""
    from huggorm_bindings import EvalState, Store

    died: list[str] = []

    def build() -> None:
        state = EvalState(Store(URI))
        canary = _Canary(died)

        def keep(v: Any) -> Any:
            # Both are CAPTURED, and that is the point. `state` is
            # what makes the cycle; `canary` is what reports whether
            # the closure was ever freed.
            assert canary is not None
            return state.make_int(v.integer())

        state.register_primop("keep", 1, keep)
        assert state.eval_expr("builtins.keep 1").integer() == 1

    build()
    gc.collect()
    gc.collect()

    assert died == ["collected"], \
        "the state and its callable survived a full collection"


def test_the_callable_still_works_while_the_state_is_reachable() -> None:
    """The fix moved the reference; it must not have weakened it.

    The callable now lives on the core and the primop carries an
    index, so a bug there would show as a callable released while
    somebody can still call it. Holding the state and collecting
    twice must change nothing."""
    from huggorm_bindings import EvalState, Store

    state = EvalState(Store(URI))
    state.register_primop("twice2", 1, lambda v: state.make_int(v.integer() * 2))
    gc.collect()
    gc.collect()
    assert state.eval_expr("builtins.twice2 21").integer() == 42


def test_a_python_function_becomes_a_nix_function(state: Any) -> None:
    """`make_primop` answers a function value that no name in
    `builtins` holds, and applying it calls Python."""
    inc = state.make_primop(
        "inc", 1, lambda v: state.make_int(v.integer() + 1))

    assert inc.is_primop()
    assert inc.primop_name() == "inc"
    assert inc(state.make_int(41)).integer() == 42
    assert state.eval_expr("builtins ? inc").boolean() is False


def test_a_primop_can_answer_a_function(state: Any) -> None:
    """The shape a primop that returns a set of functions needs: the
    inner function closes over the outer argument."""
    def adder(a: Any) -> Any:
        return state.make_primop(
            "add", 1, lambda b: state.make_int(a.integer() + b.integer()))

    state.register_primop("adder", 1, adder)

    assert state.eval_expr("builtins.adder 40 2").integer() == 42
    assert state.eval_expr("map (builtins.adder 1) [ 1 2 ]").to_json(False) \
        == "[2,3]"


def test_a_made_function_names_itself_in_its_errors(state: Any) -> None:
    """The label is the given name, not `builtins.<name>`, because
    `builtins` does not hold it."""
    wrong = state.make_primop("wrong", 1, lambda v: 42)

    with pytest.raises(Exception) as caught:
        wrong(state.make_int(1)).integer()
    assert "implementation of wrong did not return a Value" \
        in str(caught.value)


def test_a_made_function_needs_an_argument(state: Any) -> None:
    with pytest.raises(ValueError, match="at least 1"):
        state.make_primop("nothing", 0, lambda: state.make_null())


def test_a_made_function_closing_over_its_state_does_not_leak_it() -> None:
    """As for a registered primop: the callable lives on the core,
    which the collector sees through `evaluator_tp_traverse`."""
    from huggorm_bindings import EvalState, Store

    died: list[str] = []

    def build() -> None:
        state = EvalState(Store(URI))
        canary = _Canary(died)

        def keep(v: Any) -> Any:
            assert canary is not None
            return state.make_int(v.integer())

        made = state.make_primop("keep", 1, keep)
        assert made(state.make_int(1)).integer() == 1

    build()
    gc.collect()
    gc.collect()

    assert died == ["collected"], \
        "the state and its callable survived a full collection"
