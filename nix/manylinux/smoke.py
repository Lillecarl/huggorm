"""What every wheel must do under its own interpreter.

The full suite imports the generator, which needs Python 3.14, so only
the cp314 wheel runs it. This runs everywhere: each compiled module
loads, a store and an evaluator work, a Nix error arrives as its
declared class, and the async and RPC layers make a real call, the RPC
one through a server process.

argv[1] names a file with the module list the generator gives, so a
wheel that lost a module fails here by name.
"""

from __future__ import annotations

import importlib
import pathlib
import sys
import tempfile

import anyio

import huggorm
from huggorm import remote
from huggorm_bindings import EvalState, Store
from huggorm_bindings.errors import MissingAttribute

URI = "dummy://?read-only=false"


def check_modules(expected_file: str) -> pathlib.Path:
    expected = sorted(pathlib.Path(expected_file).read_text().split())
    for name in expected:
        importlib.import_module(f"huggorm_bindings.{name}")
    package = pathlib.Path(importlib.import_module("huggorm_bindings").__file__).parent
    shipped = sorted(p.name.split(".")[0] for p in package.glob("*.so"))
    if shipped != expected:
        raise RuntimeError(f"the wheel ships {shipped}, the generator names {expected}")
    return package


def check_sync() -> None:
    store = Store(URI)
    printed = store.print_store_path(store.add_to_store("hello", b"hello\n"))
    if not printed.startswith("/nix/store/") or not printed.endswith("-hello"):
        raise RuntimeError(f"add_to_store gave {printed}")
    state = EvalState(store)
    if (two := state.eval_expr("1 + 1").integer()) != 2:
        raise RuntimeError(f"1 + 1 evaluated to {two}")
    try:
        state.eval_expr("{ foo = 1; }").get("fo")
    except MissingAttribute:
        pass
    else:
        raise RuntimeError("a missing attribute raised nothing")


async def check_async() -> None:
    store = huggorm.AsyncStore(URI)
    try:
        printed = await store.print_store_path(await store.add_to_store("hello", b"hello\n"))
    finally:
        await store.aclose()
    if not printed.endswith("-hello"):
        raise RuntimeError(f"AsyncStore.add_to_store gave {printed}")


async def wait_socket(path: pathlib.Path) -> None:
    with anyio.fail_after(20):
        while True:
            try:
                stream = await anyio.connect_unix(path)
            except OSError:
                await anyio.sleep(0.05)
            else:
                await stream.aclose()
                return


async def check_rpc() -> None:
    path = pathlib.Path(tempfile.mkdtemp()) / "s"
    argv = [sys.executable, "-m", "huggorm.server", str(path)]
    async with await anyio.open_process(argv, stdout=None, stderr=None) as server:
        try:
            await wait_socket(path)
            async with remote.connect(path) as client:
                store = await client.acquire("Store", URI)
                added = await store.add_to_store("hello", b"hello\n")
                printed = await store.print_store_path(added)
        finally:
            server.terminate()
    if not printed.endswith("-hello"):
        raise RuntimeError(f"RPCStore.add_to_store gave {printed}")


def main(expected_file: str) -> None:
    package = check_modules(expected_file)
    check_sync()
    anyio.run(check_async)
    anyio.run(check_rpc)
    print(f"python {sys.version.split()[0]}: bindings from {package}, sync, async and rpc ok")


if __name__ == "__main__":
    main(sys.argv[1])
