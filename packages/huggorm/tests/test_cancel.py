"""Cancelling a call stops the Nix work under it.

`cancel_request` marks a request cancelled, and the hook
`begin_request` installs makes Nix's next `checkInterrupt` on that
thread raise `Interrupted`. The async runtime calls it when the
awaiting task is cancelled (tasks/097).

The slow work is a Python primop that sleeps, reached once per list
element, so how long the uncancelled work takes is a number here and
not a property of the machine. `builtins.toJSON` calls
`checkInterrupt` once per element (`value-to-json.cc`).
"""

import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import pytest

URI = "dummy://"
STEP = 0.01
ELEMENTS = 1000  # 10s of work at STEP, uncancelled
SLOW = f"builtins.toJSON (builtins.genList (x: builtins.spin x) {ELEMENTS})"
CANCEL_AFTER = 0.2
# Far below the 10s the work takes, and far above one STEP.
STOPPED_WITHIN = 2.0


class Spin:
    """A primop that takes `delay` seconds per call, changeable later."""

    def __init__(self, state: Any) -> None:
        self.delay = STEP
        self.calls = 0
        state.register_primop("spin", 1, self)
        self._state = state

    def __call__(self, x: Any) -> Any:
        self.calls += 1
        time.sleep(self.delay)
        return self._state.make_int(x.integer())


@pytest.fixture
def state() -> Any:
    from huggorm_bindings import EvalState

    return EvalState(URI)


@pytest.fixture
def request_id() -> Iterator[int]:
    """This thread inside one request, and the request left clean."""
    from huggorm_bindings import begin_request, end_request, forget_request

    request = 424242
    previous = begin_request(request)
    try:
        yield request
    finally:
        end_request(previous)
        forget_request(request)


def cancel_later(request: int) -> threading.Timer:
    from huggorm_bindings import cancel_request

    timer = threading.Timer(CANCEL_AFTER, cancel_request, (request,))
    timer.start()
    return timer


def test_a_cancelled_request_stops_its_evaluation(
        state: Any, request_id: int) -> None:
    from huggorm_bindings.errors import Interrupted

    spin = Spin(state)
    cancel_later(request_id)
    started = time.monotonic()
    with pytest.raises(Interrupted, match="interrupted"):
        state.eval_expr(SLOW)
    took = time.monotonic() - started
    assert took < STOPPED_WITHIN, took
    assert spin.calls < ELEMENTS, "the work ran to its end"


def test_interrupted_is_not_an_exception() -> None:
    """`except Exception` must not swallow a cancellation."""
    from huggorm_bindings.errors import Interrupted

    assert not issubclass(Interrupted, Exception)
    assert issubclass(Interrupted, BaseException)


def test_another_request_is_not_stopped(state: Any, request_id: int) -> None:
    from huggorm_bindings import cancel_request, forget_request

    spin = Spin(state)
    spin.delay = 0
    cancel_request(request_id + 1)
    try:
        got = state.eval_expr(SLOW)
    finally:
        forget_request(request_id + 1)
    assert spin.calls == ELEMENTS
    assert got.string_value().startswith("[0,1,2")


def test_an_interrupted_value_evaluates_again(
        state: Any, request_id: int) -> None:
    """Nix caches an error in the thunk that raised it, and it cached
    an interruption too, so every later force of `a` rethrew it. The
    patch `nix-interrupted-thunk-recovers.patch` keeps a recovery
    thunk instead."""
    from huggorm_bindings import forget_request
    from huggorm_bindings.errors import Interrupted

    spin = Spin(state)
    root = state.eval_expr(f"{{ a = {SLOW}; }}")
    cancel_later(request_id).join()
    with pytest.raises(Interrupted):
        state.force(root.get("a"))
    forget_request(request_id)

    spin.delay = 0
    again = root.get("a")
    state.force(again)
    assert again.string_value().startswith("[0,1,2")


async def test_a_cancelled_await_stops_the_thread() -> None:
    """The await returning is not enough. The evaluator's thread has to
    stop too, or the next call queues behind the abandoned work."""
    from huggorm_generated import AsyncEvalState

    def spin(x: Any) -> Any:
        time.sleep(STEP)
        return x

    state = AsyncEvalState(URI)
    # The primop, not a big pure list: 12M elements finished inside the
    # bound in the sandbox, so this passed with a hook that ignored
    # every cancel.
    await state.register_primop("spin", 1, spin)
    started = time.monotonic()
    with anyio.move_on_after(CANCEL_AFTER) as scope:
        await state.eval_expr(SLOW)
    assert scope.cancelled_caught, "the work ended before the cancel"
    got = await state.eval_expr("1 + 1")
    assert await got.integer() == 2
    assert time.monotonic() - started < STOPPED_WITHIN
    await state.aclose()
