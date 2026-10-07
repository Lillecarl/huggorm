"""NARs as byte streams Python reads and writes (huggorm#149).

`Source` and `Sink` are what `Store.nar_from_path` and
`Store.add_to_store_nar` take, and what a Python store's NAR hooks get.
"""

import itertools
from typing import Any

import pytest

from huggorm_bindings import (
    LayeredStore,
    LayeredStoreConfig,
    Sink,
    Source,
    Store,
    register_store_implementation,
)

NAR_MAGIC = b"\x0d\x00\x00\x00\x00\x00\x00\x00nix-archive-1"
_names = itertools.count()


class Collect(Sink):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.parts.append(data)

    def nar(self) -> bytes:
        return b"".join(self.parts)


class Chunks(Source):
    def __init__(self, data: bytes, size: int = 7) -> None:
        super().__init__()
        self.data, self.size = data, size

    def read(self, n: int) -> bytes:
        out = self.data[:min(n, self.size)]
        self.data = self.data[len(out):]
        return out


def dummy() -> Any:
    return Store("dummy://?read-only=false")


def opened(cls: type, underlying: Any = None) -> Any:
    name = f"nar-stream-test-{next(_names)}"

    def factory(config: LayeredStoreConfig) -> Any:
        return cls(config, underlying)

    register_store_implementation(name, [name], factory)
    return Store(f"{name}://")


def test_a_nar_goes_out_and_back_through_python() -> None:
    src = dummy()
    held = src.add_to_store("held", b"held")
    out = Collect()
    src.nar_from_path(held, out)
    assert out.nar().startswith(NAR_MAGIC)

    dst = dummy()
    dst.add_to_store_nar(src.query_path_info(held), Chunks(out.nar()),
                         check_sigs=False)
    assert dst.is_valid_path(held)


def test_a_source_must_not_answer_more_than_asked() -> None:
    class Greedy(Source):
        def read(self, n: int) -> bytes:
            return b"x" * (n + 1)

    src = dummy()
    held = src.add_to_store("held", b"held")
    with pytest.raises(Exception, match="when asked for at most"):
        dummy().add_to_store_nar(src.query_path_info(held), Greedy(),
                                 check_sigs=False)


def test_a_stream_with_no_override_has_nothing_below() -> None:
    with pytest.raises(Exception, match="nothing below it"):
        Sink().write(b"x")
    with pytest.raises(Exception, match="nothing below it"):
        Source().read(1)


def test_nix_copies_out_of_a_python_store() -> None:
    """The store holds its NARs in a dict. Nix's copy asks it for path
    info and for the NAR, and both answers come from Python."""
    src = dummy()
    held = src.add_to_store("held", b"held")
    info = src.query_path_info(held)
    nar = Collect()
    src.nar_from_path(held, nar)

    class Memory(LayeredStore):
        def query_path_info(self, path: Any) -> Any:
            return info

        def nar_from_path(self, path: Any, sink: Any) -> None:
            sink.write(nar.nar())

    dst = dummy()
    opened(Memory).copy_closure(dst, [held], check_sigs=False)
    assert dst.is_valid_path(held)


def test_nix_copies_into_a_python_store() -> None:
    src = dummy()
    held = src.add_to_store("held", b"held")
    expected = Collect()
    src.nar_from_path(held, expected)
    got: list[tuple[Any, bytes]] = []

    class Takes(LayeredStore):
        def is_valid_path(self, path: Any) -> bool:
            return False

        def query_valid_paths(self, paths: list[Any]) -> list[Any]:
            return []

        def add_to_store_nar(self, info: Any, source: Any,
                             repair: bool = False,
                             check_sigs: bool = True) -> None:
            parts = []
            while part := source.read(1 << 16):
                parts.append(part)
            got.append((info.path(), b"".join(parts)))

    src.copy_closure(opened(Takes), [held], check_sigs=False)
    assert got == [(held, expected.nar())]


def test_super_streams_below() -> None:
    src = dummy()
    held = src.add_to_store("held", b"held")
    seen: list[str] = []

    class Passes(LayeredStore):
        def nar_from_path(self, path: Any, sink: Any) -> None:
            seen.append("out")
            super().nar_from_path(path, sink)

        def add_to_store_nar(self, info: Any, source: Any,
                             repair: bool = False,
                             check_sigs: bool = True) -> None:
            seen.append("in")
            super().add_to_store_nar(info, source, repair, check_sigs)

    below = dummy()
    src.copy_closure(opened(Passes, below), [held], check_sigs=False)
    assert below.is_valid_path(held)
    dst = dummy()
    opened(Passes, below).copy_closure(dst, [held], check_sigs=False)
    assert dst.is_valid_path(held)
    assert seen == ["in", "out"]


def test_a_stream_ends_with_its_call() -> None:
    src = dummy()
    held = src.add_to_store("held", b"held")
    kept: list[Any] = []

    class Keeps(LayeredStore):
        def nar_from_path(self, path: Any, sink: Any) -> None:
            kept.append(sink)
            super().nar_from_path(path, sink)

    opened(Keeps, src).copy_closure(dummy(), [held], check_sigs=False)
    with pytest.raises(Exception, match="call that has returned"):
        kept[0].write(b"late")
