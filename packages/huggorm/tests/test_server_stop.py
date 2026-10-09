"""A server stops on SIGTERM or SIGINT, and closes what it holds
(huggorm#143)."""

import contextlib
import os
import pathlib
import signal
import sys

import anyio
import pytest
from conftest import socket_path, wait_socket

from huggorm import remote

# Pure: Nix checks for an interrupt once per function call (huggorm#158).
PURE = ("let sum = builtins.foldl' (a: b: a + b) 0; "
        "range = n: builtins.genList (x: x) n; in "
        "builtins.foldl' (a: _: a + sum (range 1000)) 0 (range 1000000)")


@pytest.mark.parametrize("busy", ["idle", "pure", "blocked"])
@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
async def test_a_signal_stops_the_server(signum: signal.Signals,
                                         busy: str,
                                         tmp_path: pathlib.Path) -> None:
    """The server closes the state it holds and exits 0, and a pure
    evaluation in flight stops. A call blocked in a system call holds
    the stop open, and a second signal ends the process at once."""
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    expression = {"pure": PURE,
                  # `open` waits for a writer, and no `checkInterrupt`
                  # reaches it.
                  "blocked": f"builtins.readFile {fifo}"}.get(busy)
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

                async def spins(expression: str) -> None:
                    with contextlib.suppress(Exception):
                        await state.eval_expr(expression)

                async with anyio.create_task_group() as tg:
                    if expression is not None:
                        tg.start_soon(spins, expression)
                        await anyio.sleep(0.5)
                    process.send_signal(signum)
                    if busy == "blocked":
                        await anyio.sleep(2)
                        assert process.returncode is None
                        process.send_signal(signum)
                    with anyio.fail_after(10):
                        code = await process.wait()
                    assert (code != 0) if busy == "blocked" else (code == 0)
                    tg.cancel_scope.cancel()
        finally:
            if process.returncode is None:
                process.kill()
