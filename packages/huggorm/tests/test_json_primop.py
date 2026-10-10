"""A primop over plain data, registered on an async evaluator."""

import json
from typing import Any

import pytest
from nixversion import ERROR_PREFIX

URI = "dummy://"


@pytest.fixture
async def state() -> Any:
    from huggorm import AsyncSession

    async with AsyncSession(URI) as session:
        yield session.eval(session.store())


async def test_plain_data_crosses_both_ways(state: Any) -> None:
    """Attrs, lists and every scalar reach `fn` as Python data, and its
    answer comes back as the same Nix structure."""
    from huggorm.jsonprimop import register_json_primop

    seen: list[Any] = []

    def echo(v: Any) -> Any:
        seen.append(v)
        return {"got": v, "n": 1.5, "t": True, "none": None}

    await register_json_primop(state, "echo", 1, echo)
    got = await state.eval_expr('builtins.echo { a = [ 1 "two" false null ]; }')

    assert seen == [{"a": [1, "two", False, None]}]
    assert json.loads(await got.to_json()) == {
        "got": {"a": [1, "two", False, None]}, "n": 1.5, "t": True, "none": None}


async def test_a_value_error_shows_bare(state: Any) -> None:
    """A rejected input reads as a Nix error with the function's message
    and no Python class name on it."""
    from huggorm.errors import NixError
    from huggorm.jsonprimop import register_json_primop

    def reject(_v: Any) -> Any:
        raise ValueError("not YAML")

    await register_json_primop(state, "reject", 1, reject)
    with pytest.raises(NixError) as caught:
        await state.eval_expr("builtins.reject 1")
    lines = [line.strip() for line in str(caught.value).splitlines() if line.strip()]
    assert lines[-1] == f"{ERROR_PREFIX}not YAML", lines


async def test_an_answer_that_is_not_data_is_refused(state: Any) -> None:
    from huggorm.jsonprimop import register_json_primop

    await register_json_primop(state, "odd", 1, lambda _v: object())
    with pytest.raises(Exception, match="not JSON data"):
        await state.eval_expr("builtins.odd 1")
