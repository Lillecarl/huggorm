"""
The frames, the build identity, the peer check and faults (huggorm#142).

Over a socketpair: what crosses is real bytes on a real Unix socket,
and no socket path is involved.
"""

import os
import socket
import struct
from collections.abc import AsyncIterator

import anyio
import pytest
from anyio.abc import SocketStream, UNIXSocketStream
from conftest import socket_path

from huggorm.protocol import (
    MAX_FRAME,
    Channel,
    Control,
    Faults,
    Op,
    ProtocolError,
    build_identity,
    peer_uid,
)


@pytest.fixture
async def pair() -> AsyncIterator[tuple[Channel, Channel]]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left = Channel(await UNIXSocketStream.from_socket(a))
    right = Channel(await UNIXSocketStream.from_socket(b))
    try:
        yield left, right
    finally:
        await left.aclose()
        await right.aclose()


@pytest.mark.anyio
async def test_a_frame_crosses_whole(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    sent = [Op.CALL, 7, 3, "h1", [b"\x00\xff", "text", None, {"k": [1.5]}]]
    await left.send(sent)
    got = await right.receive()
    assert got == sent and got[0] is Op.CALL


@pytest.mark.anyio
async def test_frames_keep_their_boundaries(
        pair: tuple[Channel, Channel]) -> None:
    """Fifty senders at once, each frame larger than the last. The
    reader runs beside them: the socket buffer holds far less than
    all fifty."""
    left, right = pair
    seen: list[int] = []

    async def read() -> None:
        for _ in range(50):
            frame = await right.receive()
            assert frame[2] == "x" * (frame[1] * 997)
            seen.append(frame[1])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            for i in range(50):
                tg.start_soon(left.send, [Op.RESULT, i, "x" * (i * 997)])
    assert sorted(seen) == list(range(50))


@pytest.mark.anyio
async def test_a_bad_frame_is_refused(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    await left.stream.send(struct.pack(">I", MAX_FRAME + 1))
    with pytest.raises(ProtocolError, match="over the"):
        await right.receive()


@pytest.mark.anyio
async def test_a_close_between_frames_is_the_end(
        pair: tuple[Channel, Channel]) -> None:
    """Both ends stop reading on EndOfStream, so a clean close must
    read as one."""
    left, right = pair
    await left.send([Op.DROP, []])
    await left.stream.send_eof()
    assert await right.receive() == [Op.DROP, []]
    with pytest.raises(anyio.EndOfStream):
        await right.receive()


@pytest.mark.anyio
async def test_an_unknown_op_is_refused(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    await left.send([99, 1])
    with pytest.raises(ProtocolError, match="no op numbered 99"):
        await right.receive()


@pytest.mark.anyio
async def test_the_peer_is_this_uid(pair: tuple[Channel, Channel]) -> None:
    left, _ = pair
    assert peer_uid(left.stream) == os.geteuid()


@pytest.mark.anyio
async def test_a_late_answer_is_dropped() -> None:
    """An answer for a call nobody waits on - one the client cancelled -
    is dropped, and the next answer still reaches its own call.

    A fake server, because a real one only sends a late answer in a
    race."""
    from huggorm import remote

    path = socket_path()

    async def fake(stream: SocketStream) -> None:
        channel = Channel(stream)
        await channel.receive()
        await channel.send([Op.WELCOME, "token", 0.0])
        call = await channel.receive()
        await channel.send([Op.RESULT, 999, "stray"])
        await channel.send([Op.RESULT, call[1], True])
        with pytest.raises(anyio.EndOfStream):
            await channel.receive()

    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, fake, None)
        with anyio.fail_after(10):
            async with remote.connect(path) as client:
                assert await client._ask(Op.CONTROL, Control.PING, []) is True
        tg.cancel_scope.cancel()


def test_the_identity_is_stable() -> None:
    first = build_identity()
    assert first == build_identity() and len(first) == 64


def test_a_declared_error_crosses_as_itself() -> None:
    """Not `str()` of each part: `info` is a record, and its parts
    cross as one (huggorm#100)."""
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import MissingAttribute

    with pytest.raises(MissingAttribute) as caught:
        EvalState(Store("dummy://")).eval_expr("{ foo = 1; }").get("fo")
    sent = caught.value
    faults = Faults()
    from huggorm.codec import pack, unpack

    rebuilt = faults.decode(unpack(pack(faults.encode(sent))))
    assert isinstance(rebuilt, MissingAttribute)
    assert rebuilt.info is not None and rebuilt == sent


def test_an_internal_error_keeps_its_cause() -> None:
    from huggorm_generated._runtime import InternalError

    faults = Faults()
    sent = InternalError("Store.get_uri failed", cause=KeyError("h1"))
    rebuilt = faults.decode(faults.encode(sent))
    assert type(rebuilt) is InternalError
    assert str(rebuilt) == "Store.get_uri failed"
    assert type(rebuilt.__cause__) is KeyError
    # A name outside builtins never reaches a constructor.
    odd = faults.decode(["internal", "m", "os.system", "x", None, None])
    assert type(odd.__cause__) is Exception
