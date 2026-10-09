"""NAR streams an async program serves to Nix (huggorm#155).

An object with an async `write` stands in for a `Sink`, and one with an
async `read` for a `Source`, in process and from a remote client.
"""

from typing import Any

import anyio
import pytest
from conftest import Server
from test_nar_stream import NAR_MAGIC, Chunks, Collect

from huggorm import remote
from huggorm_bindings import Store
from huggorm_generated import AsyncStore

DUMMY = "dummy://?read-only=false"


class Written:
    """An async sink. `on_write` runs before each write, on the loop."""

    def __init__(self, on_write: Any = None) -> None:
        self.parts: list[bytes] = []
        self.on_write = on_write

    async def write(self, data: bytes) -> None:
        if self.on_write is not None:
            await self.on_write(data)
        self.parts.append(data)

    def nar(self) -> bytes:
        return b"".join(self.parts)


class Read:
    """An async source over `data`, `size` bytes at most per read."""

    def __init__(self, data: bytes, size: int = 7) -> None:
        self.data, self.size = data, size

    async def read(self, n: int) -> bytes:
        out = self.data[:min(n, self.size)]
        self.data = self.data[len(out):]
        return out


def held() -> tuple[Any, Any, bytes]:
    """A store, a path it holds, and that path's NAR."""
    store = Store(DUMMY)
    path = store.add_to_store("held", b"held" * 1000)
    nar = Collect()
    store.nar_from_path(path, nar)
    return store, path, nar.nar()


async def test_a_nar_goes_out_to_an_async_sink() -> None:
    _, path, nar = held()
    store = AsyncStore(DUMMY)
    await store.add_to_store("held", b"held" * 1000)
    out = Written()
    with anyio.fail_after(30):
        await store.nar_from_path(path, out)
    assert out.nar() == nar
    assert nar.startswith(NAR_MAGIC)


async def test_a_nar_comes_in_from_an_async_source() -> None:
    sync, path, nar = held()
    store = AsyncStore(DUMMY)
    with anyio.fail_after(30):
        await store.add_to_store_nar(sync.query_path_info(path), Read(nar),
                                     check_sigs=False)
        assert await store.is_valid_path(path)


async def test_a_sync_sink_still_runs_on_the_worker() -> None:
    _, path, nar = held()
    store = AsyncStore(DUMMY)
    await store.add_to_store("held", b"held" * 1000)
    out = Collect()
    await store.nar_from_path(path, out)
    assert out.nar() == nar


async def test_a_failing_write_fails_the_call() -> None:
    async def fails(data: bytes) -> None:
        raise RuntimeError("the disk is full")

    _, path, _ = held()
    store = AsyncStore(DUMMY)
    await store.add_to_store("held", b"held" * 1000)
    with anyio.fail_after(30), pytest.raises(Exception) as caught:
        await store.nar_from_path(path, Written(fails))
    assert "the disk is full" in str(caught.value.__cause__)


async def test_a_write_may_call_the_pool() -> None:
    """The write awaits another pool call while the worker waits."""
    _, path, nar = held()
    store = AsyncStore(DUMMY)
    await store.add_to_store("held", b"held" * 1000)

    async def asks(data: bytes) -> None:
        assert await store.is_valid_path(path)

    out = Written(asks)
    with anyio.fail_after(30):
        await store.nar_from_path(path, out)
    assert out.nar() == nar


async def test_a_cancelled_call_frees_a_writer_that_hangs() -> None:
    writing, stopped = anyio.Event(), anyio.Event()

    async def hangs(data: bytes) -> None:
        writing.set()
        try:
            await anyio.sleep_forever()
        finally:
            stopped.set()

    _, path, _ = held()
    store = AsyncStore(DUMMY)
    await store.add_to_store("held", b"held" * 1000)

    async def dump() -> None:
        await store.nar_from_path(path, Written(hangs))

    with anyio.fail_after(30):
        async with anyio.create_task_group() as tg:
            tg.start_soon(dump)
            await writing.wait()
            tg.cancel_scope.cancel()
        await stopped.wait()
        assert await store.is_valid_path(path)


async def test_the_server_streams_a_nar_to_the_client(
        callback_server: Server) -> None:
    sync, path, nar = held()
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        with anyio.fail_after(30):
            await store.add_to_store_nar(sync.query_path_info(path),
                                         Chunks(nar, 4096), check_sigs=False)
            out = Written()
            await store.nar_from_path(path, out)
    assert out.nar() == nar


async def test_the_server_reads_a_nar_from_an_async_client_source(
        callback_server: Server) -> None:
    sync, path, nar = held()
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        with anyio.fail_after(30):
            await store.add_to_store_nar(sync.query_path_info(path),
                                         Read(nar, 4096), check_sigs=False)
            assert await store.is_valid_path(path)
