"""One scope for local async Nix objects (first slice).

A session hands out stores and evaluators, refuses a store from
another session, and closes what it made. `dummy://` is in-memory,
so every test here is hermetic.
"""

import pytest

from huggorm.session import AsyncSession, AsyncSessionLike


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
