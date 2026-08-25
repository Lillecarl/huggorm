"""
Fixtures for the cythonix suites.

anyio, not asyncio: anyio's pytest plugin is the runner, and the
harness here uses anyio primitives throughout. The library below it is
still on asyncio - grpclib is - and that is a separate migration
(tasks/035).

Three fixtures matter, and each exists because something here is
expensive or slow to set up:

- a gRPC server is a SUBPROCESS, so its failures are visible instead of
  swallowed by a task, and so a native crash from the binding layer
  kills the server rather than the test run. That matters more against
  real Nix than it did against the mock.
- a SHORT-TTL server is separate from the normal one. Lifetime tests
  have to wait out a sweep, and making every other test wait with them
  would be minutes of sleeping for no reason.
- grpcurl is the external-tool arm: it holds only the descriptor this
  build emitted, so it catches a schema that is wrong in a way Python
  round-trips happily past.
"""

import glob
import os
import shutil
import socket
import sys
from collections.abc import AsyncIterator, Iterator
from typing import Any

import anyio
import anyio.abc
import pytest
from anyio.streams.text import TextReceiveStream

HOST = "127.0.0.1"


@pytest.fixture(scope="session")
def ambient_store() -> Any:
    """The machine's OWN store, for tests marked `live`.

    "auto" is whatever the ambient configuration says - usually the
    daemon. A build sandbox has none of that, which is the whole
    reason the marker exists (tasks/037). Session-scoped: opening a
    store is a connection, and one is enough."""
    from cythonix_bindings import Store

    return Store("auto")

# The lifetime suite waits out a sweep. Short enough to be quick, long
# enough that a slow machine does not reap a connection mid-test.
SHORT_TTL = 3.0


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    """One backend, one event loop, for the whole session.

    Session-scoped here on purpose: anyio caches its test runner at
    this fixture's scope, so widening it is what lets a server fixture
    outlive a single test. A client belongs to the loop it connected
    on, and reconnecting is what several of these tests are ABOUT."""
    return "asyncio"


def free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


async def wait_port(port: int, timeout: float = 20) -> None:
    with anyio.fail_after(timeout):
        while True:
            try:
                stream = await anyio.connect_tcp(HOST, port)
            except OSError:
                await anyio.sleep(0.05)
            else:
                await stream.aclose()
                return


class Server:
    """One running server, and the log it produced."""

    def __init__(self, port: int, process: Any, logs: list[str]) -> None:
        self.port = port
        self.process = process
        self.logs = logs

    @property
    def tail(self) -> str:
        return "".join(self.logs[-20:])


async def _serve(ttl: float | None) -> AsyncIterator[Server]:
    """Start a server, drain its output, and stop it on the way out.

    The drain runs in a task group whose scope encloses the yield, so
    the reader cannot outlive the fixture - which is the whole point of
    structured concurrency and the reason a bare fire-and-forget task
    is not used here."""
    port = free_port()
    argv = [sys.executable, "-m", "cythonix.server", HOST, str(port)]
    if ttl is not None:
        argv.append(str(ttl))
    logs: list[str] = []

    async with await anyio.open_process(
        argv, stdout=None, stderr=None
    ) as process, anyio.create_task_group() as tg:

        async def drain(stream: Any) -> None:
            async for text in TextReceiveStream(stream, errors="replace"):
                logs.extend(text.splitlines(keepends=True))

        if process.stdout is not None:
            tg.start_soon(drain, process.stdout)
        if process.stderr is not None:
            tg.start_soon(drain, process.stderr)
        await wait_port(port)
        try:
            yield Server(port, process, logs)
        finally:
            process.terminate()
            with anyio.move_on_after(5):
                await process.wait()
            else_killed = process.returncode is None
            if else_killed:
                process.kill()
            tg.cancel_scope.cancel()


@pytest.fixture(scope="session")
async def server() -> AsyncIterator[Server]:
    """The default server: a long lease TTL, so nothing is swept while
    a test is looking away."""
    async for s in _serve(None):
        yield s


@pytest.fixture(scope="session")
async def ttl_server() -> AsyncIterator[Server]:
    """A server that reaps quickly, for the lifetime suite alone."""
    async for s in _serve(SHORT_TTL):
        yield s


@pytest.fixture
async def client(server: Server) -> AsyncIterator[Any]:
    """A connected, bound client. Pinging stops on the way out so the
    next test does not inherit a background task."""
    from cythonix import remote

    with anyio.fail_after(10):
        c = await remote.connect(HOST, server.port)
    yield c
    c.stop_pinging()


@pytest.fixture
def grpcurl() -> str:
    """The external tool, or a skip. It reads the descriptor this build
    emitted and nothing else, which is what keeps the schema honest."""
    found = os.environ.get("GRPCURL") or shutil.which("grpcurl")
    if not found:
        cands = sorted(glob.glob("/nix/store/*-grpcurl-*/bin/grpcurl"))
        found = cands[-1] if cands else None
    if not found:
        pytest.skip("grpcurl not on PATH")
    return found


@pytest.fixture
def manifest() -> Iterator[dict[str, Any]]:
    from cythonix import grpc_pb

    yield grpc_pb.load_manifest()


async def run_tool(binpath: str, port: int, symbol: str | None = None,
                   payload: str | None = None,
                   timeout: float = 20) -> tuple[int | None, str, str]:
    """grpcurl [-d payload] host:port [symbol]"""
    argv = [binpath, "-plaintext"]
    if payload is not None:
        argv += ["-d", payload]
    argv.append(f"{HOST}:{port}")
    if symbol:
        argv.append(symbol)
    with anyio.fail_after(timeout):
        done = await anyio.run_process(argv, check=False)
    return done.returncode, done.stdout.decode(), done.stderr.decode()
