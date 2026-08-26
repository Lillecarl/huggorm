"""
The real nix::Store (tasks/015).

Abstract in C++, and its implementation is chosen by a URI, so the
binding is constructed through openStore rather than a constructor.
"dummy://" is an in-memory store and needs nothing on disk, which is
what makes this testable in a build sandbox.
"""

from declare import binding, produced, wire_value


@produced(by="Store.query_path_info")
@binding(threading="pool", blocking=False)
@wire_value()
class PathInfo:
    """What a store knows about one path it holds.

    A VALUE, not a handle: it is what the store said at the moment it
    was asked, so it crosses the wire as a copy and nothing about it
    can go stale in a way a caller could act on.

    Produced, never constructed. Every field comes from the store's
    own database, so there is nothing a caller could correctly build
    one from - which `_produced` says out loud rather than leaving to
    an inference elsewhere."""

    def path(self) -> "StorePath":
        """The path this describes."""

    def nar_hash(self) -> str:
        """The hash of the path's NAR serialisation, algorithm first:
        `sha256:<base32>`, the same spelling `nix path-info` prints."""

    def nar_size(self) -> int:
        """The size of that NAR in bytes. Not the size on disk."""

    def deriver(self) -> "StorePath | None":
        """The .drv that built this, or None.

        None is a real answer, not a gap: a path added straight to the
        store was not built by anything."""

    def registration_time(self) -> int:
        """When the store learnt about this path, as a Unix time."""

    def ultimate(self) -> bool:
        """Whether this store built it itself, as opposed to receiving
        it from a substituter or an import."""

    def ca(self) -> "str | None":
        """How this path's content addresses itself, or None.

        `fixed:r:sha256:<hash>` for a path added to the store, which
        is the same spelling `nix path-info --json` prints. None for a
        path that was BUILT: an input-addressed output is named after
        the derivation that made it, not after its own bytes, so
        there is nothing to address by."""

    def references(self) -> "list[StorePath]":
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field."""

    def sigs(self) -> "list[str]":
        """Who vouched for this path, as `<key-name>:<base64>`."""


@produced(by="Store.to_store_path")
@binding(threading="pool", blocking=False)
@wire_value()
class StoreLocation:
    """Where one file sits: which store path holds it, and where
    inside.

    What `Store.to_store_path` answers. A store path names an OBJECT,
    and a file inside that object is not one - so the answer is a pair
    and both halves are needed to reach the file again.

    A VALUE, like PathInfo, and produced rather than constructed: it
    is the result of a split that only a store can perform, because
    only a store knows its own directory."""

    def path(self) -> "StorePath":
        """The store path that holds the file."""

    def sub_path(self) -> str:
        """Where the file sits inside it, leading slash included:
        `/bin/python3`.

        Empty when the path given WAS the store path. That is a real
        answer rather than a gap - there is nothing below it."""
