"""A closed evaluator lets its store go, after a path literal or an import.

Both held their accessor - and through the store accessor mounted under
it, the Store - for the life of the process (huggorm#128). A path literal
lives in an arena that runs no destructors, and the `ExprParseFile` an
`import` allocates is a `gc` object, which runs none either. A caught
error holds its position until a collection finalizes it. A daemon
store then kept one connection per closed evaluator, and a long-lived
process ran the daemon out of file descriptors.

A chroot store holds its database open while it lives, so the count of
this process's descriptors under the chroot says whether it was freed.
"""

import gc
import os
import pathlib

import pytest

from huggorm import AsyncSession


def _open_under(root: pathlib.Path) -> int:
    count = 0
    for fd in os.listdir("/proc/self/fd"):
        try:
            if os.readlink(f"/proc/self/fd/{fd}").startswith(str(root)):
                count += 1
        except OSError:
            pass
    return count


@pytest.mark.parametrize("expr", [
    # A path literal: `ExprPath`.
    "builtins.path {{ path = {source}; }}",
    # An `import`: the `ExprParseFile` evalFile allocates.
    "(import {source}).x",
    # An error a file raises and `tryEval` catches: a finalizable Boehm
    # block that holds the error's position, released by the collection
    # `aclose` runs.
    "(import {source}).caught",
])
async def test_a_closed_evaluator_releases_its_store(
        tmp_path: pathlib.Path, expr: str) -> None:
    root = tmp_path / "chroot"
    source = tmp_path / "source.nix"
    source.write_text('{ x = 1; caught = (builtins.tryEval (throw "no")).success; }')
    counts = []
    for _ in range(3):
        async with AsyncSession(f"local?root={root}") as session:
            state = session.eval(session.store())
            await state.eval_expr(expr.format(source=source))
        # The names outlive the block, and would keep this round's store.
        del state, session
        gc.collect()
        counts.append(_open_under(root))
    assert counts == [0, 0, 0], counts
