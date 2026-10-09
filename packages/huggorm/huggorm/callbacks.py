"""
EXPERIMENTAL: the server calling objects a client keeps (huggorm#153).

A class marked `@calls_back` crosses from a client as the client's id
for its object. The server holds a STUB in its place: a subclass of
the binding class whose `@virtual` methods send a CALLBACK frame and
wait for the client's REPLY or REFUSE. Nix calls the stub from the
thread that needs the answer, such as an `EvalState`'s own thread, so
that thread waits for a round trip per call.

Off unless the server runs with `--experimental-callbacks` and the
client connects with `experimental_callbacks=True`. Accepted, by
decision: a slow or gone client stalls the state that called it, a
callback that calls back into the same state deadlocks, and a state
that holds one client's objects is in practice tied to that client.
"""

from __future__ import annotations

import functools
import importlib
import itertools
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import anyio
import anyio.from_thread
import anyio.lowlevel

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
    """One connection's calls into the client's objects. Server side."""

    def __init__(self, channel: Channel, codec: Codec, faults: Faults) -> None:
        self.channel = channel
        self.codec = codec
        self.faults = faults
        # Taken on the loop, so a thread anyio did not start can reach it.
        self._loop = anyio.lowlevel.current_token()
        self._ids = itertools.count(1)
        self._waiting: dict[int, _Waiting] = {}
        self._stubs: dict[int, Any] = {}
        self._closed = False

    def obj(self, cls: str, client_id: int) -> Any:
        """The stub for the client's object `client_id`, made once."""
        stub = self._stubs.get(client_id)
        if stub is None:
            stub = self._stubs[client_id] = _stub_class(cls)(self, client_id)
        return stub

    def call(self, client_id: int, spec: Call, args: tuple[Any, ...]) -> Any:
        """Call one method of the client's object and wait for the
        answer. From a worker thread, never from the loop."""
        raw = [self.codec.encode(a.type, v, _no_proxy)
               for a, v in zip(spec.args, args, strict=True)]
        frame = anyio.from_thread.run(self._ask, client_id, spec.name, raw,
                                      token=self._loop)
        match frame:
            case [Op.REPLY, _, value]:
                return self.codec.decode(spec.returns, value, _no_proxy)
            case [Op.REFUSE, _, fault]:
                raise self.faults.decode(fault)
        raise ClientGone("the client closed the connection during a callback")

    async def _ask(self, client_id: int, name: str, raw: list[Any]) -> list[Any] | None:
        if self._closed:
            return None
        n = next(self._ids)
        waiting = self._waiting[n] = _Waiting()
        try:
            await self.channel.send([Op.CALLBACK, n, client_id, name, raw])
            await waiting.done.wait()
        finally:
            self._waiting.pop(n, None)
        return waiting.frame

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
def _stub_class(cls: str) -> type[Any]:
    """A subclass of the binding class whose every `@virtual` calls the
    client. A class attribute per method, because nanobind's trampoline
    finds an override on the type."""
    base: type = getattr(importlib.import_module("huggorm_bindings"), cls)

    def __init__(self: Any, callbacks: Callbacks, client_id: int) -> None:
        base.__init__(self)
        self._callbacks = callbacks
        self._client_id = client_id

    def method(spec: Call) -> Callable[..., Any]:
        def call(self: Any, *args: Any) -> Any:
            return self._callbacks.call(self._client_id, spec, args)
        call.__name__ = spec.name
        return call

    body = {"__init__": __init__,
            **{s.name: method(s) for s in CALLBACKS[cls]}}
    return types.new_class(f"Client{cls}", (base,),
                           exec_body=lambda ns: ns.update(body))
