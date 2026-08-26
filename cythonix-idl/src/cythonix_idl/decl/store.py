"""
The real nix::Store (tasks/015).

Abstract in C++, and its implementation is chosen by a URI, so the
binding is constructed through openStore rather than a constructor.
"dummy://" is an in-memory store and needs nothing on disk, which is
what makes this testable in a build sandbox.
"""

from cythonix_idl.declare import (
    Bint,
    Str,
    binding,
    cxx_body,
    cxx_name,
    header,
    instant,
    produced,
    wire_value,
)


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
        there is nothing to address by.

        None rather than "": the two are different answers, and this
        is the first optional SCALAR field the wire can carry them
        both across (tasks/048)."""

    def references(self) -> "list[StorePath]":
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field. Nix scans the
        bytes for them at add time, so a path added from a directory
        of plain text has none.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""

    def sigs(self) -> "list[str]":
        """Who vouched for this path, as `<key-name>:<base64>`.

        Empty for a path this store added itself: a signature says a
        path came from somewhere and arrived intact, and a local add
        travelled nowhere."""


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


@produced(by="open_store")
@header("nix/store/store-api.hh")
@binding(
    cxx="nix::Store",
    # A store carries its own locking, so any pool thread will do.
    threading="pool",
    # It talks to a daemon or a database. Every call can wait.
    blocking=True,
)
class Store:
    """A real nix::Store, opened from a URI.

    `Store("dummy://")` is in-memory. `Store("auto")` is whatever the
    ambient configuration says, which usually means the daemon."""

    @cxx_name("isValidPath")
    def is_valid_path(self, path: "StorePath") -> Bint:
        """Whether the store has that path."""

    @cxx_name("printStorePath")
    def print_store_path(self, path: "StorePath") -> Str:
        """This path as the store spells it: its directory, then the
        base name.

        The store's directory, not this machine's. A chroot store
        keeps `/nix/store` in its paths while its files live under a
        root somewhere else, so this is what the store calls the path
        and `real_path` is where the bytes are."""

    # Reads a string the config already holds. Releasing the GIL
    # around it would cost two thread-state transitions to save
    # nothing.
    @instant
    @cxx_body("return s.config.getHumanReadableURI();")
    def get_uri(self) -> Str:
        """How this store describes itself.

        For logging only, upstream is explicit about that: it does
        not round-trip as a store reference and it is not a cache
        key.

        There is no getUri() any more. 2.34 moved it onto the config
        as getHumanReadableURI, and Store reaches its config by
        reference - which a pxd cannot describe without declaring the
        whole config type for the sake of one string."""

    @cxx_body("return s.followLinksToStore(path).string();")
    def follow_links_to_store(self, path: Str) -> Str:
        """Follow symlinks until the path lands in the store, and
        stop there.

        The first half of `follow_links_to_store_path`, and the half
        that keeps what the other one drops. A `result` symlink
        pointing at a package resolves to `<store path>/bin/foo`; the
        other call answers with the store path alone.

        A str, not a pathlib.Path, and the difference is real. The
        answer is in the STORE's terms - the same spelling
        `print_store_path` gives - so its directory is the store
        directory, which a chroot store keeps at `/nix/store` while
        its files live under `<root>/nix/store`. `real_path` is the
        call that answers where the bytes are on THIS machine, and it
        returns a path because it can.

        The symlinks are read on the machine the store runs on. In
        process that is here; over RPC it is the server's filesystem,
        which is what makes this a remote call worth having - and the
        same meaning `add_path_to_store` already carries.

        Raises BadStorePath when the links run out somewhere else."""
