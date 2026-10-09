"""File trees Python serves to Nix (huggorm#152).

A `SourceAccessor` subclass answers four hooks, and Nix reads the tree
through them: `add_accessor_to_store` dumps it as a NAR, and
`EvalState.mount` shows it to the evaluator at its store path.
"""

import itertools
import json
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
_names = itertools.count()


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

    def read_directory(self, path: str) -> dict[str, FileType | None]:
        prefix = path.rstrip("/") + "/"
        names = {p[len(prefix):].split("/")[0]
                 for p in [*self.tree, *self._dirs()]
                 if p.startswith(prefix) and p != prefix}
        entries: dict[str, FileType | None] = {}
        for name in sorted(names):
            stat = self.maybe_lstat(prefix + name)
            assert stat is not None
            entries[name] = stat.type()
        return entries

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
    assert memory.read_directory("/") == {
        "bin": FileType.DIRECTORY, "empty": FileType.DIRECTORY,
        "hello.txt": FileType.REGULAR, "link": FileType.SYMLINK}


def test_an_entry_with_no_type_is_read_as_one_to_lstat() -> None:
    class NamesOnly(Memory):
        def read_directory(self, path: str) -> dict[str, FileType | None]:
            return dict.fromkeys(super().read_directory(path))

    store = dummy()
    assert store.add_accessor_to_store("tree", NamesOnly(TREE)) == (
        store.add_accessor_to_store("tree", Memory(TREE)))


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


SOURCE: dict[str, Any] = {
    "/default.nix": b"""{
  imported = import ./sub/x.nix;
  joined = builtins.readFile (./. + "/hello.txt");
  listed = builtins.readDir ./.;
  missing = builtins.pathExists ./absent;
  linked = builtins.readFile ./link;
}
""",
    "/sub/x.nix": b"41 + 1\n",
    "/hello.txt": b"hello\n",
    "/link": "hello.txt",
}


@pytest.mark.parametrize("pure", [False, True])
def test_the_evaluator_reads_a_mounted_tree(pure: bool) -> None:
    """Path arithmetic and pure evaluation both reach the mount
    (huggorm#153)."""
    from huggorm_bindings import EvalState

    store = dummy()
    state = EvalState(store, {"pure-eval": str(pure).lower()})
    path = state.mount(Memory(SOURCE))
    assert path == dummy().add_accessor_to_store("source", Memory(SOURCE))
    entry = f"{store.print_store_path(path)}/default.nix"
    got = json.loads(state.eval_file(entry).to_json())
    assert got == {
        "imported": 42,
        "joined": "hello\n",
        "listed": {"default.nix": "regular", "hello.txt": "regular",
                   "link": "symlink", "sub": "directory"},
        "missing": False,
        "linked": "hello\n",
    }


def test_mounting_reads_without_adding() -> None:
    from huggorm_bindings import EvalState

    store = dummy()
    path = EvalState(store).mount(Memory(SOURCE))
    assert not store.is_valid_path(path)


def test_interpolating_a_mounted_path_copies_it_through_python() -> None:
    from huggorm_bindings import EvalState

    store = dummy()
    state = EvalState(store)
    path = state.mount(Memory(SOURCE))
    sub = f"{store.print_store_path(path)}/sub"
    copied = state.eval_expr(f'"${{{sub}}}"').string_value()
    added = store.add_accessor_to_store("sub", Memory(SOURCE), "/sub")
    assert copied == store.print_store_path(added)


def opened(cls: type, underlying: Any = None) -> Any:
    """A `LayeredStore` subclass over `underlying`, through a fresh scheme."""
    from huggorm_bindings import LayeredStoreConfig, register_store_implementation

    name = f"accessor-test-{next(_names)}"

    def factory(config: LayeredStoreConfig) -> Any:
        return cls(config, underlying)

    register_store_implementation(name, [name], factory)
    return Store(f"{name}://")


def test_python_reads_a_store_object() -> None:
    store = dummy()
    held = store.add_accessor_to_store("tree", Memory(TREE))
    tree = store.get_fs_accessor(held)
    assert tree is not None
    assert tree.read_file("/hello.txt") == b"hello\n"
    stat = tree.maybe_lstat("/bin/run")
    assert stat is not None and stat.is_executable()
    assert tree.read_link("/link") == "hello.txt"
    assert tree.read_directory("/") == {
        "bin": "directory", "empty": "directory",
        "hello.txt": "regular", "link": "symlink"}
    assert tree.maybe_lstat("/absent") is None
    absent = store.parse_store_path(
        "/nix/store/00000000000000000000000000000000-absent")
    assert store.get_fs_accessor(absent) is None


def test_a_layer_over_a_store_object() -> None:
    """A virtual file over a real store object: Python reads the object
    through Nix, and Nix reads the layer through Python."""

    class Greeting(SourceAccessor):
        def read_file(self, path: str) -> bytes:
            if path == "/hello.txt":
                return b"hi\n"
            return super().read_file(path)

        def maybe_lstat(self, path: str) -> Stat | None:
            if path == "/hello.txt":
                return Stat(FileType.REGULAR, 3)
            return super().maybe_lstat(path)

    store = dummy()
    held = store.add_accessor_to_store("tree", Memory(TREE))
    layered = store.add_accessor_to_store(
        "tree", Greeting(store.get_fs_accessor(held)))
    assert layered == store.add_accessor_to_store(
        "tree", Memory({**TREE, "/hello.txt": b"hi\n"}))


def test_a_python_store_falls_through_to_the_store_below() -> None:
    from huggorm_bindings import LayeredStore

    below = dummy()
    held = below.add_accessor_to_store("tree", Memory(TREE))
    tree = Store.get_fs_accessor(opened(LayeredStore, below), held)
    assert tree is not None and tree.read_file("/hello.txt") == b"hello\n"


def test_the_evaluator_reads_a_python_store() -> None:
    """A store path no store holds: the Python store answers its files,
    and the evaluator reads them through the store's whole-store
    accessor."""
    from huggorm_bindings import EvalState, LayeredStore

    path = dummy().add_accessor_to_store("source", Memory(SOURCE))
    asked: list[bool] = []

    class Virtual(LayeredStore):
        def get_fs_accessor(self, wanted: Any,
                            require_valid_path: bool = True) -> SourceAccessor | None:
            asked.append(require_valid_path)
            return Memory(SOURCE) if wanted == path else None

    store = opened(Virtual, dummy())
    state = EvalState(store)
    entry = f"{store.print_store_path(path)}/default.nix"
    assert json.loads(state.eval_file(entry).to_json())["imported"] == 42
    assert asked == [False]


def test_mounting_at_a_given_path_reads_nothing_until_evaluation() -> None:
    """A remote client hashes its own tree and sends the path, so the
    server does not read every file over the connection to hash it."""
    from huggorm_bindings import EvalState

    reads: list[str] = []

    class Counted(Memory):
        def maybe_lstat(self, path: str) -> Stat | None:
            reads.append(path)
            return super().maybe_lstat(path)

    store = dummy()
    path = dummy().add_accessor_to_store("source", Memory(SOURCE))
    state = EvalState(store)
    assert state.mount(Counted(SOURCE), path=path) == path
    assert reads == []
    entry = f"{store.print_store_path(path)}/default.nix"
    assert json.loads(state.eval_file(entry).to_json())["imported"] == 42
    assert reads


def test_a_given_path_the_store_holds_keeps_the_store_content() -> None:
    """A caller cannot mount other files over a real store object."""
    from huggorm_bindings import EvalState

    store = dummy()
    path = store.add_accessor_to_store("source", Memory(SOURCE))
    state = EvalState(store)
    state.mount(Memory({**SOURCE, "/sub/x.nix": b"0\n"}), path=path)
    entry = f"{store.print_store_path(path)}/default.nix"
    assert json.loads(state.eval_file(entry).to_json())["imported"] == 42
