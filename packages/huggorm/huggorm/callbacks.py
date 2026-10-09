"""
EXPERIMENTAL: the server calling objects a client keeps (huggorm#153).

A class marked `@calls_back` crosses from a client as the client's id
for its object. The server holds a PROXY in its place: an object whose
`@virtual` methods are coroutines that send a CALLBACK frame and await
the client's REPLY or REFUSE. It is an `Async<X>` object like any async
program's, so the runtime's `adapt` gives it to Nix the same way, and a
hook may call back into the state that reads it (huggorm#155).

Off unless the server runs with `--experimental-callbacks` and the
client connects with `experimental_callbacks=True`. Accepted, by
decision: a slow or gone client stalls the call that reads it, and a
state that holds one client's objects is in practice tied to that
client.
"""

from __future__ import annotations

import contextlib
import functools
import itertools
import types
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

import anyio

from huggorm_generated._callspec import Call
from huggorm_generated._policy import CALLBACKS

from .codec import Codec
from .protocol import Channel, Faults, Op


def _no_proxy(_: Any) -> Any:
    raise TypeError("a callback carries no handle")


@dataclass
class _Waiting:
    done: anyio.Event = field(default_factory=anyio.Event)
    frame: list[Any] | None = None


class ClientGone(Exception):
    """The client closed its connection, so a callback has no answer."""


class Callbacks:
    """One connection's calls into the client's objects. Server side,
    on the loop."""

    def __init__(self, channel: Channel, codec: Codec, faults: Faults) -> None:
        self.channel = channel
        self.codec = codec
        self.faults = faults
        self._ids = itertools.count(1)
        self._waiting: dict[int, _Waiting] = {}
        self._proxies: dict[int, Any] = {}
        self._closed = False

    def obj(self, cls: str, client_id: int) -> Any:
        """The proxy for the client's object `client_id`, made once."""
        proxy = self._proxies.get(client_id)
        if proxy is None:
            proxy = self._proxies[client_id] = _proxy_class(cls)(
                self, client_id)
        return proxy

    async def call(self, client_id: int, spec: Call,
                   args: tuple[Any, ...]) -> Any:
        """Call one method of the client's object, and answer what the
        client answers."""
        raw = [self.codec.encode(a.type, v, _no_proxy)
               for a, v in zip(spec.args, args, strict=True)]
        if self._closed:
            raise ClientGone("the client closed the connection")
        n = next(self._ids)
        waiting = self._waiting[n] = _Waiting()
        try:
            await self.channel.send([Op.CALLBACK, n, client_id, spec.name, raw])
            await waiting.done.wait()
        except BaseException:
            if not waiting.done.is_set():
                # `send` shields itself, so a cancelled caller still sends.
                with contextlib.suppress(anyio.BrokenResourceError,
                                         anyio.ClosedResourceError):
                    await self.channel.send([Op.CANCEL, n])
            raise
        finally:
            self._waiting.pop(n, None)
        match waiting.frame:
            case [Op.REPLY, _, value]:
                return self.codec.decode(spec.returns, value, _no_proxy)
            case [Op.REFUSE, _, fault]:
                raise self.faults.decode(fault)
        raise ClientGone("the client closed the connection during a callback")

    def answer(self, frame: list[Any]) -> None:
        """A REPLY or REFUSE from the client."""
        waiting = self._waiting.get(frame[1])
        if waiting is not None:
            waiting.frame = frame
            waiting.done.set()

    def close(self) -> None:
        """The connection ended: every waiting callback fails."""
        self._closed = True
        for waiting in self._waiting.values():
            waiting.done.set()


@functools.cache
def _proxy_class(cls: str) -> type:
    """A class with one coroutine per `@virtual` of `cls`, each a
    callback to the client."""

    def __init__(self: Any, callbacks: Callbacks, client_id: int) -> None:
        self._callbacks = callbacks
        self._client_id = client_id

    def method(spec: Call) -> Callable[..., Coroutine[Any, Any, Any]]:
        async def call(self: Any, *args: Any) -> Any:
            return await self._callbacks.call(self._client_id, spec, args)
        call.__name__ = spec.name
        return call

    namespace = {"__init__": __init__,
                 **{s.name: method(s) for s in CALLBACKS[cls]}}
    return types.new_class(f"Client{cls}", (), {},
                           lambda ns: ns.update(namespace))
