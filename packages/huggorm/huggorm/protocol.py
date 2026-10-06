"""
The frames huggorm speaks on its Unix socket.

A frame is a u32 big-endian length, then a msgpack array whose first
element is an `Op`. Values inside a frame are `codec` shapes.

Both ends run the same build, so nothing here negotiates. HELLO
carries the build identity, and a server refuses any other
(huggorm#142). Only the server's own uid may connect.

    HELLO     [Op, identity, claim | None]
    WELCOME   [Op, token, lease_ttl]
    CALL      [Op, call_id, index, handle | None, [arg, ...]]
    CONTROL   [Op, call_id, Control, [arg, ...]]
    CANCEL    [Op, call_id]
    DROP      [Op, [handle, ...]]                     one way
    RESULT    [Op, call_id, value]
    FAULT     [Op, call_id, fault]

`call_id` is the client's, and a RESULT or FAULT carries it back, so
calls on one connection may finish in any order. A CALL names its call
by its index in the emitted `CALLS`.
"""

from __future__ import annotations

import builtins
import enum
import hashlib
import importlib
import importlib.util
import pathlib
import socket
import struct
from types import ModuleType
from typing import Any

import anyio
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from huggorm_generated._callspec import Wire, WireKind
from huggorm_generated._policy import ERROR_FIELDS, ERROR_MODULE

from .codec import Codec, pack, unpack

# A frame larger than this is a bug or an attack. The largest real
# answer is a store's whole path list, far below it.
MAX_FRAME = 256 << 20
_LENGTH = struct.Struct(">I")


class Op(enum.IntEnum):
    HELLO = 0
    WELCOME = 1
    CALL = 2
    CONTROL = 3
    CANCEL = 4
    DROP = 5
    RESULT = 6
    FAULT = 7


class Control(enum.IntEnum):
    """The session operations: everything that is not a declared call."""

    PING = 0
    SHARE = 1
    DETACH = 2
    RELEASE = 3
    REALIZE = 4
    LOGS_BARRIER = 5
    NEXT_LOGS = 6
    NEXT_PROCESS_LOGS = 7
    CLOSE_LOGS = 8


class ProtocolError(Exception):
    """The peer sent something this build never writes."""


def build_identity() -> str:
    """A digest of every Python source both ends run.

    The generated package holds the call table and the value shapes,
    and this package holds the frames and the session. Two ends that
    agree on all of it agree on the protocol, so nothing finer than a
    digest is needed."""
    digest = hashlib.sha256()
    for name in ("huggorm", "huggorm_generated"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"{name} is not importable")
        root = pathlib.Path(spec.origin).parent
        for path in sorted(root.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def peer_uid(stream: SocketStream) -> int:
    """The uid of the process on the other end of a Unix socket, as
    the kernel recorded it at connect time."""
    sock: socket.socket = stream.extra(SocketAttribute.raw_socket)
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


class Channel:
    """Frames over one connected stream. Sends are serialised, because
    calls on one connection answer from many tasks."""

    def __init__(self, stream: SocketStream) -> None:
        self.stream = stream
        self._reader = BufferedByteReceiveStream(stream)
        self._send_lock = anyio.Lock()

    async def send(self, frame: list[Any]) -> None:
        body = pack(frame)
        if len(body) > MAX_FRAME:
            raise ProtocolError(f"a {len(body)} byte frame is over the "
                                f"{MAX_FRAME} byte limit")
        async with self._send_lock:
            await self.stream.send(_LENGTH.pack(len(body)) + body)

    async def receive(self) -> list[Any]:
        """The next frame, as `[Op, ...]`. Raises EndOfStream when the
        peer closed between frames."""
        (length,) = _LENGTH.unpack(await self._reader.receive_exactly(4))
        if length > MAX_FRAME:
            raise ProtocolError(f"a {length} byte frame is over the "
                                f"{MAX_FRAME} byte limit")
        frame = unpack(await self._reader.receive_exactly(length))
        if type(frame) is not list or not frame or type(frame[0]) is not int:
            raise ProtocolError(f"a frame arrived as {frame!r:.80}")
        try:
            frame[0] = Op(frame[0])
        except ValueError:
            raise ProtocolError(f"no op numbered {frame[0]}") from None
        return frame

    async def aclose(self) -> None:
        await self.stream.aclose()


# -- faults --------------------------------------------------------------
_ERRORS = tuple(ERROR_FIELDS)


class Faults:
    """A failure as one msgpack value, and back.

        [code, message, cause_type, cause_message, error, parts]

    A DECLARED error crosses as itself: `error` indexes the declared
    error classes and `parts` are its parts, and the far side rebuilds
    that class, so `except BadStorePath` works over the wire
    (huggorm#66). Anything else crosses as an InternalError: its cause
    crosses as itself when declared, and by builtin name when not."""

    def __init__(self, codec: Codec | None = None,
                 module: ModuleType | None = None) -> None:
        self.codec = codec or Codec()
        self._module = module

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            self._module = importlib.import_module(ERROR_MODULE)
        return self._module

    def _declared(self, err: BaseException | None) -> int | None:
        """The index of `err`'s class among the declared errors. By
        identity: a class that only shares a declared name is not it."""
        if err is None:
            return None
        name = type(err).__name__
        if name not in ERROR_FIELDS:
            return None
        if type(err) is not getattr(self.module, name, None):
            return None
        return _ERRORS.index(name)

    def _parts(self, index: int, err: BaseException) -> Any:
        return self.codec.encode(Wire(WireKind.ERROR, _ERRORS[index]), err,
                                 _no_proxy)

    def encode(self, wrapper: Any) -> list[Any]:
        """`wrapper` is a declared error or a WrapperError."""
        itself = self._declared(wrapper)
        if itself is not None:
            return [wrapper.code, wrapper.message, "", "", itself,
                    self._parts(itself, wrapper)]
        cause: BaseException | None = getattr(wrapper, "__cause__", None)
        if cause is None:
            return [wrapper.code, wrapper.message, "", "", None, None]
        declared = self._declared(cause)
        return [wrapper.code, wrapper.message, type(cause).__name__,
                str(cause), declared,
                None if declared is None else self._parts(declared, cause)]

    def decode(self, raw: Any) -> BaseException:
        from huggorm_generated._runtime import InternalError, WrapperError

        if type(raw) is not list or len(raw) != 6:
            raise ProtocolError(f"a fault arrived as {raw!r:.80}")
        _code, message, cause_type, cause_message, error, parts = raw
        rebuilt = None
        if error is not None:
            if type(error) is not int or not 0 <= error < len(_ERRORS):
                raise ProtocolError(f"no declared error numbered {error!r}")
            rebuilt = self.codec.decode(
                Wire(WireKind.ERROR, _ERRORS[error]), parts, _no_proxy)
        if not cause_type:
            return rebuilt or WrapperError(message)
        return InternalError(
            message, cause=rebuilt or _approximate(cause_type, cause_message))


def _no_proxy(_: Any) -> Any:
    raise TypeError("an error's part is never a handle")


def _approximate(type_name: str, message: str) -> BaseException:
    """A cause nothing declared, rebuilt from its builtin name.

    The name must resolve in `builtins` AND be an exception class, so
    no other name the peer sends reaches a constructor. One whose
    constructor wants more than a message, such as UnicodeDecodeError,
    becomes an Exception that names it."""
    kls = getattr(builtins, type_name, None)
    if isinstance(kls, type) and issubclass(kls, BaseException):
        try:
            return kls(message)
        except Exception:
            pass
    return Exception(f"{type_name}: {message}" if type_name else message)
