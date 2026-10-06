"""Runaway recursion on an async evaluator raises; it does not crash.

Nix's default `max-call-depth` needs about 27 MB of C stack, and a thread
the runtime starts would otherwise keep the 8 MiB `RLIMIT_STACK` gives it.
Then the stack runs out before Nix's counter fires, and the process takes
SIGSEGV. A child process runs the recursion, so a regression fails this
test instead of taking the suite down with it.
"""

import subprocess
import sys

CHILD = """
import anyio
from huggorm import AsyncSession
from huggorm.errors import NixError

async def main():
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        try:
            await state.eval_expr("let f = n: f (n + 1); in f 0")
        except NixError as e:
            print("raised:", e)

anyio.run(main)
"""


def test_runaway_recursion_raises_rather_than_crashing() -> None:
    done = subprocess.run(
        [sys.executable, "-c", CHILD], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert "raised:" in done.stdout and "stack overflow" in done.stdout, done.stdout
