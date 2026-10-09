"""
nix::Source and nix::Sink: `huggorm_decl/cpp/serialise.hpp`.

Byte streams, as Nix moves a NAR (huggorm#149). A subclass overrides
`read` or `write`, and Nix reads or writes through it:

    class Collect(Sink):
        def __init__(self):
            super().__init__()
            self.parts = []

        def write(self, data):
            self.parts.append(data)

    store.nar_from_path(path, Collect())

A Python store's override gets one Nix made, which reads or writes the
stream Nix holds. It is valid only during that call; after it, `read`
and `write` raise.

An async program passes an object with an async `write` or `read`
instead (`AsyncSink`, `AsyncSource`), and so does a remote client:
the method runs on the loop of the call that reads it (huggorm#155).
"""

from huggorm_dsl.declare import (
    U64,
    Bytes,
    binding,
    calls_back,
    header,
    in_process,
    posted,
    virtual,
)


@in_process
@calls_back
@header("huggorm_decl/cpp/serialise.hpp")
@binding(cxx="huggorm::Sink", threading="pool", blocking=True)
class Sink:
    """Where Nix writes bytes."""

    def __init__(self) -> None:
        """A sink a subclass makes: `write` is the subclass's."""

    @virtual
    @posted
    def write(self, data: Bytes) -> None:
        """Take the next bytes."""


@in_process
@calls_back
@header("huggorm_decl/cpp/serialise.hpp")
@binding(cxx="huggorm::Source", threading="pool", blocking=True)
class Source:
    """Where Nix reads bytes from."""

    def __init__(self) -> None:
        """A source a subclass makes: `read` is the subclass's."""

    @virtual
    def read(self, n: U64) -> Bytes:
        """At most `n` bytes, and at least one. `b""` ends the stream."""
