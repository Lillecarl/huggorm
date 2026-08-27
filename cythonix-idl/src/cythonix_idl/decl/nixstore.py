"""
nix::Store, the shape that blocks.

StorePath and ValidPathInfo both read memory the object already owns.
This one talks to a daemon or a database, so every call can wait - and
that single declared fact, `blocking=True`, is what makes the emitter
write `nb::call_guard<nb::gil_scoped_release>()`. One decision, and
the declaration never names the backend that reads it.

Three shapes appear here for the first time:

**A parameter of a BOUND type.** `is_valid_path` genuinely takes a
StorePath, and the emitter resolves `nix::StorePath` through path.py
rather than either file repeating it.

**A default argument.** Python's own syntax carries it, and the
emitter spells `false` where Python wrote `False`. Dropping one would
silently change the signature a caller sees.

**No constructor.** nix::Store is abstract and chosen by a URI, so
there is nothing for `nb::init` to bind. `@produced(by=...)` says so,
and says where to look instead.
"""

from cythonix_idl.declare import (
    Bint,
    Cxx,
    Str,
    binding,
    blocks,
    cxx_name,
    header,
    instant,
    produced,
)


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
    """A Nix store, opened from a URI.

    Abstract in C++ and chosen by a URI, so it is opened rather than
    constructed. `dummy://` is in-memory and needs nothing on disk,
    which is what makes it testable in a build sandbox."""

    @cxx_name("isValidPath")
    def is_valid_path(self, path: "StorePath") -> Bint:
        """Whether the store holds this path."""

    @cxx_name("queryPathFromHashPart")
    def query_path_from_hash_part(self, hash: Str) -> "StorePath | None":
        """The path with this 32-character hash part, or None."""

    # `followLinksToStorePath` resolves symlinks, which is filesystem
    # work rather than a store query - it can wait even on a store
    # whose other calls do not, so it would carry @blocks on its own.
    @blocks
    @cxx_name("followLinksToStorePath")
    def follow_links_to_store_path(self, path: Str) -> "StorePath":
        """Resolve a path through symlinks to the store path holding
        it."""

    # A guard C++ owes Python and the declaration cannot state: the
    # empty string is not a bad store path to parseStorePath, it
    # ABORTS the process. So the check lives with the call.
    def parse_store_path(self, path: Str) -> "StorePath":
        """Parse a full store path into a StorePath.

        Raises rather than aborting when given an empty string, which
        is the one thing upstream will not do for us."""
        Cxx("""
if (path.empty())
    throw nix::BadStorePath("parse_store_path: store path must not be empty");
return self.parseStorePath(path);
        """)

    # Reads a string the config already holds.
    @instant
    def get_store_dir(self) -> Str:
        """The directory this store keeps its objects in."""
        Cxx("return self.config.storeDir_;")

    @instant
    def get_uri(self, with_params: Bint = False) -> Str:
        """The URI this store was opened from.

        `with_params=True` includes the query parameters, which carry
        settings a caller may have passed at open time."""
        Cxx("return nix::StoreReference::parse(self.config.getReference()"
            ".render(with_params)).render(with_params);")
