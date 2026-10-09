"""Remote evaluation of the client's files (huggorm#153). EXPERIMENTAL.

The client keeps a `SourceAccessor` and hands it to `EvalState.mount`
on the server. The server calls the accessor back over the connection
for every read, from the evaluation thread.
"""

import json
import pathlib
from typing import Any

import anyio
import pytest
from conftest import Server
from test_source_accessor import SOURCE, Memory

from huggorm import remote
from huggorm_bindings import Stat, Store

DUMMY = "dummy://?read-only=false"


def local_path(tree: dict[str, Any]) -> Any:
    """The store path of `tree`, hashed in this process."""
    return Store(DUMMY).add_accessor_to_store("source", Memory(tree))


class Counted(Memory):
    def __init__(self, tree: dict[str, Any]) -> None:
        super().__init__(tree)
        self.reads: list[str] = []

    def maybe_lstat(self, path: str) -> Stat | None:
        self.reads.append(path)
        return super().maybe_lstat(path)


async def evaluated(state: Any, store: Any, path: Any) -> Any:
    entry = f"{await store.print_store_path(path)}/default.nix"
    with anyio.fail_after(30):
        return json.loads(await (await state.eval_file(entry)).to_json())


async def test_the_server_evaluates_the_clients_files(
        callback_server: Server) -> None:
    tree = Counted(SOURCE)
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        state = await client.acquire("EvalState", store)
        path = local_path(SOURCE)
        assert await state.mount(tree, "source", path) == path
        assert tree.reads == []
        got = await evaluated(state, store, path)
    assert got["imported"] == 42
    assert got["joined"] == "hello\n"
    assert got["listed"]["sub"] == "directory"
    assert "/sub/x.nix" in tree.reads


async def test_mounting_without_a_path_hashes_through_the_client(
        callback_server: Server) -> None:
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        state = await client.acquire("EvalState", store)
        with anyio.fail_after(30):
            path = await state.mount(Memory(SOURCE))
        assert path == local_path(SOURCE)
        assert (await evaluated(state, store, path))["imported"] == 42


async def test_a_failing_client_hook_fails_the_call(
        callback_server: Server) -> None:
    class Broken(Memory):
        def read_file(self, path: str) -> bytes:
            raise RuntimeError(f"cannot read {path}")

    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        with anyio.fail_after(30), pytest.raises(Exception,
                                                 match="callback read_file"):
            await store.add_accessor_to_store("tree", Broken(SOURCE))


async def test_a_client_that_leaves_mid_callback_fails_the_call(
        callback_server: Server) -> None:
    """The waiting thread fails rather than waits forever, so the
    server goes on serving."""

    class Leaves(Memory):
        client: Any = None

        def read_file(self, path: str) -> bytes:
            self.client._close()
            return super().read_file(path)

    tree = Leaves(SOURCE)
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        tree.client = client
        store = await client.acquire("Store", DUMMY)
        with anyio.fail_after(30), pytest.raises(remote.ConnectionLost):
            await store.add_accessor_to_store("tree", tree)

    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        with anyio.fail_after(30):
            assert await store.add_accessor_to_store(
                "source", Memory(SOURCE)) == local_path(SOURCE)


async def test_a_client_without_the_flag_refuses_before_sending(
        client: Any) -> None:
    state = await client.acquire("EvalState",
                                 await client.acquire("Store", DUMMY))
    with pytest.raises(TypeError, match="enables experimental callbacks"):
        await state.mount(Memory(SOURCE))


async def test_a_server_without_the_flag_refuses(server: Server) -> None:
    async with remote.connect(server.path,
                              experimental_callbacks=True) as client:
        state = await client.acquire(
            "EvalState", await client.acquire("Store", DUMMY))
        with anyio.fail_after(30), pytest.raises(Exception) as caught:
            await state.mount(Memory(SOURCE))
    assert "--experimental-callbacks" in str(caught.value.__cause__)


async def test_the_server_evaluates_a_directory_on_the_client(
        callback_server: Server, tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import filesystem_accessor

    root = tmp_path / "project"
    (root / "sub").mkdir(parents=True)
    (root / "default.nix").write_text(
        '{ answer = import ./sub/x.nix; text = builtins.readFile ./hello.txt; }')
    (root / "sub" / "x.nix").write_text("41 + 1")
    (root / "hello.txt").write_text("hello\n")
    path = Store(DUMMY).compute_store_path("source", str(root))
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", DUMMY)
        state = await client.acquire("EvalState", store)
        assert await state.mount(filesystem_accessor(root), "source",
                                 path) == path
        got = await evaluated(state, store, path)
    assert got == {"answer": 42, "text": "hello\n"}


@pytest.mark.usefixtures("flakes")
async def test_the_server_locks_a_flake_on_the_client(
        callback_server: Server, tmp_path: pathlib.Path) -> None:
    """The tree is copied to the server's store, then locked as the
    `path:` flake of that store path. The flake's own `path:` inputs
    still name the server's disk."""
    from huggorm_bindings import filesystem_accessor, parse_flake_ref

    root = tmp_path / "project"
    root.mkdir()
    (root / "flake.nix").write_text(
        '{ description = "client"; outputs = _: { x = import ./x.nix; }; }')
    (root / "x.nix").write_text("41 + 1")
    async with remote.connect(callback_server.path,
                              experimental_callbacks=True) as client:
        store = await client.acquire("Store", str(tmp_path / "server"))
        state = await client.acquire("EvalState", store,
                                     {"flake-registry": ""})
        with anyio.fail_after(30):
            path = await store.add_accessor_to_store(
                "source", filesystem_accessor(root))
            ref = parse_flake_ref(f"path:{await store.print_store_path(path)}")
            locked = await state.lock_flake(ref, write_lock_file=False)
            assert await locked.description() == "client"
            outputs = await state.call_flake(locked)
            await state.force(outputs)
            x = await outputs.get("x")
            await state.force(x)
            assert await x.integer() == 42
