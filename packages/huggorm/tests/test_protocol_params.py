"""A proxy parameter is spelled as its protocol on every surface.

The type cannot say where an object lives, so each side checks at the
call: in process a remote handle is refused, and over the wire an
in-process object is refused (huggorm#26). A consumer typed against
the protocols then drives either location with the same code.
"""

from typing import Any

import pytest

from huggorm_generated import AsyncEvalState, AsyncStore
from huggorm_generated.protocols import EvalStateLike, ValueLike


async def _remote_value(client: Any) -> Any:
    state = await client.acquire(
        "EvalState", await client.acquire("Store", "dummy://"))
    return state, await state.eval_expr("1 + 1")


async def _apply_twice(state: EvalStateLike, fn: ValueLike,
                       arg: ValueLike) -> int:
    """One consumer, typed only against the protocols."""
    once = await fn(arg)
    twice = await fn(once)
    await state.force(twice)
    return await twice.integer()


async def test_one_consumer_drives_both_locations(client: Any) -> None:
    local = AsyncEvalState(AsyncStore("dummy://"))
    try:
        fn = await local.eval_expr("x: x * 2")
        arg = await local.eval_expr("3")
        assert await _apply_twice(local, fn, arg) == 12
    finally:
        await local.aclose()

    remote, _ = await _remote_value(client)
    fn = await remote.eval_expr("x: x * 2")
    arg = await remote.eval_expr("3")
    assert await _apply_twice(remote, fn, arg) == 12


async def test_in_process_refuses_a_remote_handle(client: Any) -> None:
    _, remote = await _remote_value(client)
    local = AsyncEvalState(AsyncStore("dummy://"))
    try:
        with pytest.raises(TypeError, match="handle on a server"):
            await local.force(remote)
    finally:
        await local.aclose()


async def test_a_remote_call_refuses_an_in_process_object(
        client: Any) -> None:
    state, _ = await _remote_value(client)
    local = AsyncEvalState(AsyncStore("dummy://"))
    try:
        value = await local.eval_expr("1")
        with pytest.raises(TypeError, match="not a handle on this server"):
            await state.force(value)
    finally:
        await local.aclose()
