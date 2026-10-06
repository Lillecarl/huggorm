"""A closed evaluator lets its store go, even after a path literal.

A parsed path literal held its accessor - and through the store accessor
mounted under it, the Store - for the life of the process, because
expressions live in an arena that runs no destructors (huggorm#128). A
daemon store then kept one connection per closed evaluator, and a
long-lived process ran the daemon out of file descriptors.

A chroot store holds its database open while it lives, so the count of
this process's descriptors under the chroot says whether it was freed.
"""

import gc
import os
import pathlib

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


async def test_a_path_literal_does_not_keep_the_store(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "chroot"
    source = tmp_path / "source.txt"
    source.write_text("x")
    counts = []
    for _ in range(3):
        async with AsyncSession(f"local?root={root}") as session:
            state = session.eval(session.store())
            await state.eval_expr(f"builtins.path {{ path = {source}; }}")
        # The names outlive the block, and would keep this round's store.
        del state, session
        gc.collect()
        counts.append(_open_under(root))
    assert counts == [0, 0, 0], counts
