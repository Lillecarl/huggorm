"""File trees an async program serves to Nix (huggorm#155).

An object with the `AsyncSourceAccessor` methods stands in for a
`SourceAccessor`. Nix calls a stub on the worker thread, and the hook
runs on the loop, in the task that awaits the call.
"""

import json
from collections.abc import Awaitable, Callable
from typing import Any

import anyio
import pytest
from test_source_accessor import SOURCE, Memory

from huggorm_bindings import FileType, Stat, Store
from huggorm_generated import AsyncEvalState, AsyncStore

DUMMY = "dummy://?read-only=false"


class Tree:
    """`Memory` behind async hooks. `on_read` runs before each file
    read, on the loop."""

    def __init__(self, tree: dict[str, Any],
                 on_read: Callable[[str], Awaitable[None]] | None = None
                 ) -> None:
        self.sync = Memory(tree)
        self.on_read = on_read
        self.threads: set[str] = set()

    async def maybe_lstat(self, path: str) -> Stat | None:
        return self.sync.maybe_lstat(path)

    async def read_directory(self, path: str) -> dict[str, FileType | None]:
        return self.sync.read_directory(path)

    async def read_link(self, path: str) -> str:
        return self.sync.read_link(path)

    async def read_file(self, path: str) -> bytes:
        if self.on_read is not None:
            await self.on_read(path)
        return self.sync.read_file(path)


def local_path() -> Any:
    return Store(DUMMY).add_accessor_to_store("source", Memory(SOURCE))


async def evaluated(state: AsyncEvalState, path: Any) -> Any:
    entry = f"{Store(DUMMY).print_store_path(path)}/default.nix"
    return json.loads(await (await state.eval_file(entry)).to_json())


async def test_a_store_reads_an_async_tree() -> None:
    """On a pool thread: the worker waits, and does nothing else."""
    with anyio.fail_after(30):
        got = await AsyncStore(DUMMY).add_accessor_to_store(
            "source", Tree(SOURCE))
    assert got == local_path()


async def test_an_evaluator_reads_an_async_tree() -> None:
    state = AsyncEvalState(AsyncStore(DUMMY))
    with anyio.fail_after(30):
        path = await state.mount(Tree(SOURCE))
        assert path == local_path()
        got = await evaluated(state, path)
    assert got["imported"] == 42
    assert got["joined"] == "hello\n"
    await state.aclose()


async def test_pure_evaluation_reads_a_mounted_tree() -> None:
    """Pure mode guards the server's own files, not a mounted tree
    (huggorm#153)."""
    state = AsyncEvalState(AsyncStore(DUMMY), {"pure-eval": "true"})
    with anyio.fail_after(30):
        path = await state.mount(Memory(SOURCE))
        got = await evaluated(state, path)
        assert got["imported"] == 42
        with pytest.raises(Exception, match="pure"):
            await state.eval_expr("builtins.readFile /etc/hostname")
    await state.aclose()


async def test_a_hook_calls_the_state_that_reads_it() -> None:
    """The state's thread waits for the hook, and runs the hook's own
    call on that thread meanwhile."""
    state = AsyncEvalState(AsyncStore(DUMMY))
    answers: list[int] = []

    async def asks(path: str) -> None:
        answers.append(await (await state.eval_expr("1 + 1")).integer())

    with anyio.fail_after(30):
        path = await state.mount(Tree(SOURCE, asks))
        assert (await evaluated(state, path))["imported"] == 42
    assert answers and set(answers) == {2}
    await state.aclose()


async def test_a_cancelled_call_stops_its_hook_and_frees_the_state() -> None:
    state = AsyncEvalState(AsyncStore(DUMMY))
    reading = anyio.Event()

    async def hangs(path: str) -> None:
        reading.set()
        await anyio.sleep_forever()

    async def mount() -> None:
        await state.mount(Tree(SOURCE, hangs))

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(mount)
            await reading.wait()
            tg.cancel_scope.cancel()
        assert await (await state.eval_expr("1")).integer() == 1
    await state.aclose()


async def test_a_failing_hook_fails_the_call() -> None:
    async def fails(path: str) -> None:
        raise RuntimeError(f"cannot read {path}")

    with anyio.fail_after(30), pytest.raises(Exception) as caught:
        await AsyncStore(DUMMY).add_accessor_to_store(
            "source", Tree(SOURCE, fails))
    assert "cannot read" in str(caught.value.__cause__)
