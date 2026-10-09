"""A server stops on SIGTERM or SIGINT, and closes what it holds
(huggorm#143)."""

import contextlib
import signal
import sys

import anyio
import pytest
from conftest import socket_path, wait_socket

from huggorm import remote


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_a_signal_stops_the_server(signum: signal.Signals,
                                         busy: bool) -> None:
    """An idle server closes the state it holds and exits 0. A call Nix
    cannot interrupt holds the stop open, and a second signal ends the
    process at once."""
    path = socket_path()
    async with await anyio.open_process(
            [sys.executable, "-m", "huggorm.server", str(path)],
            stdout=None, stderr=None) as process:
        try:
            await wait_socket(path)
            async with remote.connect(path) as client:
                store = await client.acquire("Store", "dummy://")
                state = await client.acquire("EvalState", store)
                assert await (await state.eval_expr("1 + 1")).integer() == 2

                async def spins() -> None:
                    # Pure evaluation: Nix never checks for an interrupt.
                    with contextlib.suppress(Exception):
                        await state.eval_expr(
                            "let sum = builtins.foldl' (a: b: a + b) 0; "
                            "range = n: builtins.genList (x: x) n; in "
                            "builtins.foldl' (a: _: a + sum (range 1000)) "
                            "0 (range 1000000)")

                async with anyio.create_task_group() as tg:
                    if busy:
                        tg.start_soon(spins)
                        await anyio.sleep(0.5)
                    process.send_signal(signum)
                    if busy:
                        await anyio.sleep(2)
                        assert process.returncode is None
                        process.send_signal(signum)
                    with anyio.fail_after(30):
                        code = await process.wait()
                    assert (code != 0) if busy else (code == 0)
                    tg.cancel_scope.cancel()
        finally:
            if process.returncode is None:
                process.kill()
