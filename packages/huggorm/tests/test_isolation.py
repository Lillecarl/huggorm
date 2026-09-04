"""An EvalState is an isolation, and these are its edges.

One state, one thread, and nothing of one state is meaningful to
another. Carl's rule, and it is a design decision rather than a
limitation - a state is the most granular parallelism `nix::EvalState`
supports, so it is the unit this repo isolates on.

WHERE it is enforced is the other half of that decision. The async
layer is the lowest one that manages threads for a caller, so it is
where a library user meets the rule - and the rpc goes through the
same wrappers, so a remote caller meets it too. A caller holding the
sync binding directly is on their own: the C++ is exactly as
permissive as libexpr, which has no such rule of its own, and the
failure there is a wrong answer rather than an exception.

`tasks/085`'s second gap and `tasks/034`'s cross-state residue are
both this file.
"""

from typing import Any

import pytest

URI = "dummy://"


async def two_states() -> tuple[Any, Any]:
    """Two evaluators, each on its own thread.

    Both touched once, because an `AffineRunner` builds its target
    lazily on its own thread and a runner with no object yet has no
    thread to compare."""
    from huggorm_generated import AsyncEvalState

    first, second = AsyncEvalState(URI), AsyncEvalState(URI)
    await first.eval_expr("1")
    await second.eval_expr("1")
    return first, second


async def test_two_states_never_share_a_thread() -> None:
    """The rule, and it holds by CONSTRUCTION rather than by a check.

    `AffineRunner.__init__` makes its own
    `ThreadPoolExecutor(max_workers=1)`, so two states cannot land on
    one thread - there is no pool to collide in. That is why the
    enforcement below is about VALUES and not about threads: the
    thread half needs nothing but this assertion.

    A caller who wants two states on one thread has to reach past the
    async layer to the sync binding, which is exactly the line Carl
    drew."""
    first, second = await two_states()
    try:
        assert first._runner.last_worker_ident is not None
        assert first._runner.last_worker_ident \
            != second._runner.last_worker_ident
        assert first._runner._executor() is not second._runner._executor()
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_value_from_another_state_is_refused() -> None:
    """The gap `tasks/034` named and left open.

    A `nix::Value` is not self-describing: an attribute name is a
    `Symbol`, an index into the producing state's own table. Handing
    one to a second state reads whatever that state's table holds at
    the same index - so the failure is a WRONG ANSWER, not a crash,
    which is why it has to be refused rather than left to fail."""
    first, second = await two_states()
    try:
        mine = await first.eval_expr("{ a = 1; }")
        with pytest.raises(TypeError, match="another EvalState"):
            await second.force(mine)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_value_from_this_state_is_not_refused() -> None:
    """The control, and it is not decoration.

    A check that refused everything would pass the test above. This
    is the half that says the rule is about WHICH state rather than
    about passing a value at all."""
    first, second = await two_states()
    try:
        mine = await first.eval_expr("{ a = 1; }")
        await first.force(mine)
    finally:
        await first.aclose()
        await second.aclose()


async def test_applying_a_foreign_function_is_refused() -> None:
    """`tasks/034` listed `apply` beside `force`, and one check covers
    both.

    Derived rather than restated: the refusal lives in `unwrap_arg`,
    which every wrapper argument already flows through, so it reaches
    all seven `Value` parameters across the five methods that take one
    - `force`, `apply`, `apply_auto`, `list_append` and `attrs_set` -
    with nothing written per method."""
    first, second = await two_states()
    try:
        fn = await first.eval_expr("x: x")
        arg = await second.eval_expr("1")
        with pytest.raises(TypeError, match="another EvalState"):
            await fn.apply(arg)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_builder_refuses_a_foreign_item() -> None:
    """The staging methods, which is where a wrong answer would be
    quietest.

    `list_append` puts a value into a list this binding built. A
    foreign one would go in without complaint and read as a member of
    the wrong state's symbol table on the way out."""
    first, second = await two_states()
    try:
        target = await first.make_list()
        foreign = await second.eval_expr("1")
        with pytest.raises(TypeError, match="another EvalState"):
            await first.list_append(target, foreign)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_child_value_is_foreign_too() -> None:
    """The chain, which is a different fact from the direct case.

    A value read out of another value gets an `AttachedRunner` whose
    parent is the FIRST value's runner, not the state's - so the
    executor identity has to hold transitively. It does, because
    `AttachedRunner._executor` returns `self._parent._executor()` all
    the way down to the `AffineRunner`'s one pool.

    Read rather than assumed, and gated here because every other
    refusal in this file passes a value the state produced directly."""
    first, second = await two_states()
    try:
        child = await (await second.eval_expr("{ a = 1; }")).get("a")
        with pytest.raises(TypeError, match="another EvalState"):
            await first.force(child)
    finally:
        await first.aclose()
        await second.aclose()


async def test_a_pool_object_is_not_an_isolation() -> None:
    """The rule is about AFFINE objects, and this says so.

    A `Store` is pool-threaded - thread-safe by declaration - so it
    belongs to no isolation, and a `StorePath` from one store reaches
    another's method with no complaint. Without this, "refuse a
    foreign argument" would read as a rule about arguments in general.

    A `StorePath` is also a wire VALUE, so it crosses as a copy. That
    is the exception Carl named - forced into data and copied - and it
    is checked before the isolation test for exactly that reason."""
    from huggorm_generated import AsyncStore

    first, second = AsyncStore(URI), AsyncStore(URI)
    try:
        path = await first.parse_store_path(
            "/nix/store/00000000000000000000000000000000-x")
        assert await second.print_store_path(path)
    finally:
        await first.aclose()
        await second.aclose()
