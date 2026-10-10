"""Cancelling a call stops the Nix work under it.

`cancel_request` marks a request cancelled, and the hook
`begin_request` installs makes Nix's next `checkInterrupt` on that
thread raise `Interrupted`. The async runtime calls it when the
awaiting task is cancelled (huggorm#97).

The slow work is a Python primop that sleeps, reached once per list
element, so how long the uncancelled work takes is a number here and
not a property of the machine. `builtins.toJSON` calls
`checkInterrupt` once per element (`value-to-json.cc`).

A pure evaluation stops too: `nix-call-function-checks-interrupt.patch`
calls `checkInterrupt` once per function call (huggorm#158).
"""

import threading
import time
from collections.abc import Iterator
from typing import Any

import anyio
import pytest
from nixversion import INTERRUPTED

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
    from huggorm_bindings import EvalState, Store

    return EvalState(Store(URI))


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
    with pytest.raises(Interrupted, match=INTERRUPTED):
        state.eval_expr(SLOW)
    took = time.monotonic() - started
    assert took < STOPPED_WITHIN, took
    assert spin.calls < ELEMENTS, "the work ran to its end"


# Pure: no primop and no printing. About 7s uncancelled on dynhetz.
PURE_SLOW = ("let sum = builtins.foldl' (a: b: a + b) 0; "
             "range = n: builtins.genList (x: x) n; in "
             "builtins.foldl' (a: _: a + sum (range 1000)) 0 (range 100000)")


def test_a_cancelled_request_stops_a_pure_evaluation(
        state: Any, request_id: int) -> None:
    """Upstream libexpr calls `checkInterrupt` only while it prints a
    value. Without the patch, this runs to its end."""
    from huggorm_bindings.errors import Interrupted

    cancel_later(request_id)
    started = time.monotonic()
    with pytest.raises(Interrupted, match=INTERRUPTED):
        state.eval_expr(PURE_SLOW)
    took = time.monotonic() - started
    assert took < STOPPED_WITHIN, took


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


@pytest.fixture
def scope_id() -> Iterator[int]:
    """This thread inside one interrupt scope, and the scope left clean."""
    from huggorm_bindings import begin_interrupt_scope, end_interrupt_scope, forget_interrupt_scope

    scope = 515151
    previous = begin_interrupt_scope(scope)
    try:
        yield scope
    finally:
        end_interrupt_scope(previous)
        forget_interrupt_scope(scope)


def test_a_cancelled_scope_stops_a_request_inside_it(
        state: Any, scope_id: int, request_id: int) -> None:
    """The scope is armed first and a request is named inside it, the
    order nanopynix uses. The request must not replace the scope.

    Perturbation: drop the `scope_cancellations()` term from the hook
    in `install_interrupt_check` and this runs the whole 10s."""
    from huggorm_bindings import cancel_interrupt_scope
    from huggorm_bindings.errors import Interrupted

    spin = Spin(state)
    threading.Timer(CANCEL_AFTER, cancel_interrupt_scope, (scope_id,)).start()
    started = time.monotonic()
    with pytest.raises(Interrupted, match=INTERRUPTED):
        state.eval_expr(SLOW)
    assert time.monotonic() - started < STOPPED_WITHIN
    assert spin.calls < ELEMENTS, "the work ran to its end"


def test_a_scope_and_a_request_with_one_number_are_two_things(
        state: Any, request_id: int) -> None:
    """Two tables: cancelling scope N does not stop request N."""
    from huggorm_bindings import cancel_interrupt_scope, forget_interrupt_scope

    spin = Spin(state)
    spin.delay = 0
    cancel_interrupt_scope(request_id)
    try:
        got = state.eval_expr(SLOW)
    finally:
        forget_interrupt_scope(request_id)
    assert spin.calls == ELEMENTS
    assert got.string_value().startswith("[0,1,2")


async def test_a_cancelled_await_stops_the_thread() -> None:
    """The await returning is not enough. The evaluator's thread has to
    stop too, or the next call queues behind the abandoned work."""
    from huggorm_generated import AsyncEvalState, AsyncStore

    def spin(x: Any) -> Any:
        time.sleep(STEP)
        return x

    state = AsyncEvalState(AsyncStore(URI))
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


# Interruptible with no primop, which has no rpc: `toJSON` calls
# `checkInterrupt` once per element. About 1s uncancelled on dynhetz.
REMOTE_SLOW = ("builtins.stringLength (builtins.toJSON "
               "(builtins.genList (x: x * x) 6000000))")


async def test_a_dropped_remote_call_stops_its_work(client: Any) -> None:
    """The rule Carl chose for huggorm#98: a client that drops a call
    stops the Nix work under it on the server, so the state's thread
    is free for the next call.

    The uncancelled work is timed in the same run, and the next call
    must answer in under half of it. A fast machine therefore cannot
    pass this by finishing the work before anyone waits on it."""
    state = await client.acquire(
        "EvalState", await client.acquire("Store", URI))
    started = time.monotonic()
    await state.eval_expr(REMOTE_SLOW)
    full = time.monotonic() - started

    with anyio.move_on_after(CANCEL_AFTER) as scope:
        await state.eval_expr(REMOTE_SLOW + " + 1")
    assert scope.cancelled_caught, "the work ended before the cancel"
    started = time.monotonic()
    got = await state.eval_expr("1 + 1")
    assert await got.integer() == 2
    waited = time.monotonic() - started
    assert waited < full / 2, (waited, full)
