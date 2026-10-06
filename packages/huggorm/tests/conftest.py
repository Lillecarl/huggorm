"""
Fixtures for the huggorm suites.

anyio, not asyncio: anyio's pytest plugin is the runner, and the
harness here uses anyio primitives throughout (huggorm#35).

Two fixtures matter, and each exists because something here is
expensive or slow to set up:

- a server is a SUBPROCESS, so its failures are visible instead of
  swallowed by a task, and so a native crash from the binding layer
  kills the server rather than the test run. That matters more against
  real Nix than it did against the mock.
- a SHORT-TTL server is separate from the normal one. Lifetime tests
  have to wait out a sweep, and making every other test wait with them
  would be minutes of sleeping for no reason.
"""

import pathlib
import sys
import tempfile
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING, Any

import anyio
import anyio.abc
import pytest
from anyio.streams.text import TextReceiveStream

if TYPE_CHECKING:
    from huggorm_gen import ir

@pytest.fixture
def flakes() -> Iterator[None]:
    """The `flakes` feature, on for one test and put back after.

    Nix parses no flake reference with it off, and neither does this.
    The setting belongs to the process, so a value left behind would
    change every later test."""
    from huggorm_bindings import get_setting, set_setting

    before = get_setting("experimental-features") or ""
    set_setting("extra-experimental-features", "flakes")
    try:
        yield
    finally:
        set_setting("experimental-features", before)


@pytest.fixture(scope="session")
def ambient_store() -> Any:
    """The machine's OWN store, for tests marked `live`.

    "auto" is whatever the ambient configuration says - usually the
    daemon. A build sandbox has none of that, which is the whole
    reason the marker exists (huggorm#37). Session-scoped: opening a
    store is a connection, and one is enough."""
    from huggorm_bindings import Store

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


def socket_path() -> pathlib.Path:
    """A fresh socket path. Short, because `sun_path` holds 108 bytes,
    and under TMPDIR, because the build sandbox has no /tmp."""
    return pathlib.Path(tempfile.mkdtemp(prefix="hg")) / "s"


async def wait_socket(path: pathlib.Path, timeout: float = 20) -> None:
    with anyio.fail_after(timeout):
        while True:
            try:
                stream = await anyio.connect_unix(path)
            except OSError:
                await anyio.sleep(0.05)
            else:
                await stream.aclose()
                return


class Server:
    """One running server, and the log it produced."""

    def __init__(self, path: pathlib.Path, process: Any,
                 logs: list[str]) -> None:
        self.path = path
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
    path = socket_path()
    argv = [sys.executable, "-m", "huggorm.server", str(path)]
    if ttl is not None:
        argv.append(str(ttl))
    # The `flakes` fixture turns the feature on in THIS process only.
    argv += ["--option", "extra-experimental-features", "flakes"]
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
        await wait_socket(path)
        try:
            yield Server(path, process, logs)
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
    """A connected, bound client, closed on the way out.

    The `async with` encloses the yield, so the client's task group -
    and the ping loop in it - cannot outlive the fixture. anyio needs
    the group entered and exited by ONE task, and a fixture body is
    one task across its yield; a test is not the same task, which is
    why this cannot be an `AsyncExitStack` a test adds clients to
    (measured, huggorm#35)."""
    from huggorm import remote

    # Entered and left by hand rather than with one `async with`,
    # because the client has to stay open across the yield and a
    # fixture cannot `async with` around one.
    #
    # NO `fail_after` around the enter, and it had one at first. A
    # client opens a task group, so entering it leaves a cancel scope
    # OPEN - and the timeout's scope then tries to close inside it:
    #
    #   RuntimeError: Attempted to exit a cancel scope that isn't the
    #   current tasks's current cancel scope
    #
    # A guard on the connect would have to close after the client
    # does, which is the opposite of what it is for. The suite's own
    # timeouts cover a server that never answers.
    opening = remote.connect(server.path)
    # `opening` and not the client is what gets closed: `connect` is a
    # generator, and exiting the client behind its back would leave
    # the generator suspended forever.
    client = await opening.__aenter__()
    try:
        yield client
    finally:
        await opening.__aexit__(None, None, None)


def load_model() -> ir.Model:
    """The declaration set as the typed model the emitters read.

    Holding emitted code against this is holding it against the
    DECLARATIONS, through the same reader the emitters use."""
    from huggorm_gen.cppgen.generate import declared_model

    return declared_model()


@pytest.fixture
def model() -> ir.Model:
    return load_model()


def _no_proxy(handle: Any) -> Any:
    raise AssertionError(f"{handle!r} crossed as a proxy")


def crossed(w: Any, value: Any) -> Any:
    """`value` as the far side reads it off the socket: encoded, packed
    and unpacked."""
    from huggorm.codec import Codec, pack, unpack

    return unpack(pack(Codec().encode(w, value, _no_proxy)))


def across(w: Any, value: Any) -> Any:
    """`value` encoded, packed, unpacked and decoded."""
    from huggorm.codec import Codec

    return Codec().decode(w, crossed(w, value), _no_proxy)


def part(cls: str, raw: list[Any], field: str) -> Any:
    """One declared part of an encoded wire value, by name."""
    from huggorm_generated._policy import WIRE_FIELDS

    return raw[[a.name for a in WIRE_FIELDS[cls]].index(field)]
