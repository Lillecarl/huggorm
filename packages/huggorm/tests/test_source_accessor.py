"""File trees Python serves to Nix (huggorm#152).

A `SourceAccessor` subclass answers four hooks, and Nix reads the tree
through them: here `add_accessor_to_store` dumps it as a NAR.
"""

import os
import pathlib
from typing import Any

import pytest

from huggorm_bindings import FileType, SourceAccessor, Stat, Store

TREE: dict[str, Any] = {
    "/hello.txt": b"hello\n",
    "/bin/run": (b"#!/bin/sh\necho run\n", True),
    "/link": "hello.txt",
    "/empty": {},
}


def dummy() -> Any:
    return Store("dummy://?read-only=false")


class Memory(SourceAccessor):
    """A tree in a dict: bytes, (bytes, executable), a symlink target as
    a str, or a dict for an empty directory."""

    def __init__(self, tree: dict[str, Any]) -> None:
        super().__init__()
        self.tree = tree

    def _dirs(self) -> set[str]:
        dirs = {"/"}
        for path, node in self.tree.items():
            if isinstance(node, dict):
                dirs.add(path)
            parent = os.path.dirname(path)
            while parent not in dirs:
                dirs.add(parent)
                parent = os.path.dirname(parent)
        return dirs

    def maybe_lstat(self, path: str) -> Stat | None:
        if path in self._dirs():
            return Stat(FileType.DIRECTORY)
        node = self.tree.get(path)
        if node is None:
            return None
        if isinstance(node, str):
            return Stat(FileType.SYMLINK)
        data, executable = node if isinstance(node, tuple) else (node, False)
        return Stat(FileType.REGULAR, len(data), executable)

    def read_directory(self, path: str) -> list[str]:
        prefix = path.rstrip("/") + "/"
        return sorted({p[len(prefix):].split("/")[0]
                       for p in [*self.tree, *self._dirs()]
                       if p.startswith(prefix) and p != prefix})

    def read_link(self, path: str) -> str:
        node = self.tree[path]
        assert isinstance(node, str)
        return node

    def read_file(self, path: str) -> bytes:
        node = self.tree[path]
        data: bytes = node[0] if isinstance(node, tuple) else node
        return data


def on_disk(root: pathlib.Path, tree: dict[str, Any]) -> pathlib.Path:
    for path, node in tree.items():
        at = root / path.lstrip("/")
        at.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(node, dict):
            at.mkdir()
        elif isinstance(node, str):
            at.symlink_to(node)
        else:
            data, executable = node if isinstance(node, tuple) else (node, False)
            at.write_bytes(data)
            at.chmod(0o755 if executable else 0o644)
    return root


def test_nix_reads_a_python_tree_as_it_reads_the_same_tree_on_disk(
        tmp_path: pathlib.Path) -> None:
    store = dummy()
    from_python = store.add_accessor_to_store("tree", Memory(TREE))
    from_disk = store.add_path_to_store("tree", str(on_disk(tmp_path / "t", TREE)))
    assert from_python == from_disk


def test_a_path_inside_the_tree(tmp_path: pathlib.Path) -> None:
    store = dummy()
    from_python = store.add_accessor_to_store("run", Memory(TREE), "/bin")
    from_disk = store.add_path_to_store("run", str(on_disk(tmp_path / "t", TREE) / "bin"))
    assert from_python == from_disk


def test_python_reads_the_hooks() -> None:
    memory = Memory(TREE)
    stat = memory.maybe_lstat("/bin/run")
    assert stat is not None
    assert (stat.type(), stat.file_size(), stat.is_executable()) == (
        FileType.REGULAR, 19, True)
    assert memory.maybe_lstat("/absent") is None
    assert memory.read_directory("/") == ["bin", "empty", "hello.txt", "link"]


def test_a_layer_overrides_one_file_and_forwards_the_rest(
        tmp_path: pathlib.Path) -> None:
    """A virtual file over a tree: the layer answers `/hello.txt`, and
    `super()` reaches the tree below for everything else."""

    class Greeting(SourceAccessor):
        def maybe_lstat(self, path: str) -> Stat | None:
            if path == "/hello.txt":
                return Stat(FileType.REGULAR, 3)
            return super().maybe_lstat(path)

        def read_file(self, path: str) -> bytes:
            if path == "/hello.txt":
                return b"hi\n"
            return super().read_file(path)

    store = dummy()
    layered = store.add_accessor_to_store("tree", Greeting(Memory(TREE)))
    changed = {**TREE, "/hello.txt": b"hi\n"}
    assert layered == store.add_path_to_store(
        "tree", str(on_disk(tmp_path / "t", changed)))


def test_a_raising_hook_is_an_error() -> None:
    class Broken(Memory):
        def read_file(self, path: str) -> bytes:
            raise RuntimeError(f"cannot read {path}")

    with pytest.raises(RuntimeError, match="cannot read /bin/run"):
        dummy().add_accessor_to_store("tree", Broken(TREE))


def test_an_accessor_with_nothing_below_raises() -> None:
    with pytest.raises(Exception, match="nothing below it"):
        SourceAccessor().read_file("/")
