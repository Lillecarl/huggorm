"""A primop over plain data: JSON in, JSON out.

`register_primop` hands a callable forced Values and wants a Value back.
Most Python worth calling from Nix - a parser, a validator - works on
plain data instead, so this converts both ways. Each argument crosses
as `realise_json`, which builds the store paths it names first, so the
function can read them. The result is built back as a Nix value, and
every string in it carries the arguments' string context: a result
read from a store path keeps the dependency on it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from huggorm_bindings.errors import EvalError
from huggorm_generated._runtime import BaseRunner

if TYPE_CHECKING:
    from collections.abc import Callable

    from huggorm_bindings import EvalState, Value
    from huggorm_generated import AsyncEvalState

type JsonValue = bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None


def _value(state: EvalState, obj: object, context: list[str]) -> Value:
    match obj:
        case None:
            return state.make_null()
        case str():
            return state.make_string(obj, context)
        # Before `int`: a bool is an int to `match` as to `isinstance`.
        case bool():
            return state.make_bool(obj)
        case int():
            return state.make_int(obj)
        case float():
            return state.make_float(obj)
        case list() | tuple():
            made = state.make_list()
            for item in obj:
                state.list_append(made, _value(state, item, context))
            return made
        case dict():
            made = state.make_attrs()
            for key, item in obj.items():
                state.attrs_set(made, str(key), _value(state, item, context))
            return made
    raise TypeError(f"a JSON primop answered a {type(obj).__name__}, which is not JSON data")


def json_primop(state: EvalState, fn: Callable[..., Any]) -> Callable[..., Value]:
    """`fn` as a callable `register_primop` takes, on `state`'s thread.

    A `ValueError` from `fn` rejects the input, so Nix shows its
    message bare, as for a C++ primop. Anything else keeps its class
    name in the message."""

    def bridge(*args: Value) -> Value:
        data = [json.loads(arg.realise_json(False)) for arg in args]
        context = sorted({element for arg in args for element in arg.string_context()})
        try:
            result = fn(*data)
        except ValueError as e:
            raise EvalError(str(e)) from e
        return _value(state, result, context)

    return bridge


async def register_json_primop(
        state: AsyncEvalState, name: str, arity: int, fn: Callable[..., Any]) -> None:
    """Publish `fn` as `builtins.<name>`, over plain data.

    The registration runs on the state's own thread, because the bridge
    builds its result with that state."""
    runner = state._backend
    if not isinstance(runner, BaseRunner):
        raise TypeError("register_json_primop runs only in process: a primop "
                        "calls Python on the evaluator's thread (huggorm#33)")
    await runner.run(lambda raw: raw.register_primop(name, arity, json_primop(raw, fn)))


__all__ = ["JsonValue", "json_primop", "register_json_primop"]
