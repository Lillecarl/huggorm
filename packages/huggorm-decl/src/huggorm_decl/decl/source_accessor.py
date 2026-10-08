"""
nix::SourceAccessor: `huggorm_decl/cpp/source_accessor.hpp`.

A file tree Nix reads (huggorm#152). A subclass answers four hooks, and
Nix reads through them wherever it takes an accessor:

    class Memory(SourceAccessor):
        def __init__(self, files):
            super().__init__()
            self.files = files

        def maybe_lstat(self, path):
            if path in self.files:
                return Stat(FileType.REGULAR, len(self.files[path]))
            ...

        def read_file(self, path):
            return self.files[path]

A path is absolute within the tree: the root is `/`. A subclass made
over another accessor forwards what it does not override, and `super()`
reaches the one below.

In process only: an accessor is a callback into this process.
"""

from huggorm_decl.decl.words import FileType
from huggorm_dsl.declare import (
    U64,
    Bint,
    Bytes,
    Cxx,
    Str,
    binding,
    header,
    in_process,
    reads,
    virtual,
)


@in_process
@header("nix/util/source-accessor.hh")
@binding(cxx="nix::SourceAccessor::Stat", threading="pool", blocking=False)
class Stat:
    """What one file system object is, without following a symlink."""

    def __init__(self, type: FileType, file_size: U64 | None = None,
                 is_executable: Bint = False) -> None:
        """Describe an object. Only a regular file has a size or an
        executable bit."""
        Cxx("""
new (self) nix::SourceAccessor::Stat{
    .type = type, .fileSize = file_size, .isExecutable = is_executable};
        """)

    @reads("type")
    def type(self) -> FileType:
        """What kind of object this is."""

    @reads("fileSize")
    def file_size(self) -> U64 | None:
        """A regular file's size, when the accessor knows it."""

    @reads("isExecutable")
    def is_executable(self) -> Bint:
        """Whether a regular file is executable."""


@in_process
@header("huggorm_decl/cpp/source_accessor.hpp")
@binding(cxx="huggorm::SourceAccessor", holder="shared_ptr",
         threading="pool", blocking=True)
class SourceAccessor:
    """A file tree Nix reads. See the module."""

    def __init__(self, below: SourceAccessor | None = None) -> None:
        """An accessor over `below`, or over nothing."""

    @virtual
    def maybe_lstat(self, path: Str) -> Stat | None:
        """What is at `path`, or None when nothing is."""

    @virtual
    def read_directory(self, path: Str) -> dict[str, FileType | None]:
        """The entries of the directory at `path`, each with its type.
        None for a type the accessor does not know: Nix asks
        `maybe_lstat` for it when it needs it."""

    @virtual
    def read_link(self, path: Str) -> Str:
        """The target of the symlink at `path`."""

    @virtual
    def read_file(self, path: Str) -> Bytes:
        """The contents of the regular file at `path`. Not a symlink's
        target: Nix follows a symlink itself."""
