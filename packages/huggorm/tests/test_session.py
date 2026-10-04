"""One scope for local async Nix objects (first slice).

A session hands out stores and evaluators, refuses a store from
another session, and closes what it made. `dummy://` is in-memory,
so every test here is hermetic.
"""

import inspect
from collections.abc import Callable
from typing import Any

import pytest

from huggorm.session import AsyncSession, AsyncSessionLike
from huggorm_generated import AsyncEvalState, AsyncStore


async def test_evaluates_through_session_stores() -> None:
    async with AsyncSession("dummy://") as session:
        store = session.store()
        state = session.eval(store)
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2
        assert isinstance(session, AsyncSessionLike)


async def test_explicit_uri_wins_over_the_default() -> None:
    async with AsyncSession("dummy://") as session:
        store = session.store("dummy://")
        assert await store.get_uri() == await session.store().get_uri()


async def test_foreign_store_is_refused() -> None:
    async with AsyncSession("dummy://") as first, AsyncSession("dummy://") as second:
        with pytest.raises(ValueError, match="another session"):
            second.eval(first.store())


async def test_close_is_idempotent() -> None:
    session = AsyncSession("dummy://")
    state = session.eval(session.store())
    await state.eval_expr("1 + 1")
    await session.aclose()
    await session.aclose()


def _params(fn: Callable[..., Any], drop: int) -> list[tuple[str, object]]:
    return [
        (name, param.default)
        for name, param in list(inspect.signature(fn).parameters.items())[drop:]
    ]


def test_session_passes_constructors_through() -> None:
    """The session adds scope, not parameters.

    `eval()` takes exactly what the generated constructor takes, so
    a declaration change that moves a signature fails here rather
    than drifting silently. `store()` names the same parameter, but
    its default is `None` (the session default) where the bare
    constructor says `'auto'`; that difference is the session, so
    the gate names it instead of forbidding it.
    """
    session_names = [n for n, _ in _params(AsyncSession.store, 1)]
    generated_names = [n for n, _ in _params(AsyncStore.__init__, 1)]
    assert session_names == generated_names
    assert _params(AsyncSession.eval, 2) == _params(AsyncEvalState.__init__, 2)
    assert _params(AsyncSessionLike.eval, 2) == _params(AsyncEvalState.__init__, 2)
