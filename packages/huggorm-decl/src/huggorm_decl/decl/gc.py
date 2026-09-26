"""
nix::GCOptions and nix::GCResults: `nix/store/gc-store.hh`.

The two halves of one call. `collectGarbage` takes an options struct
by const reference and fills a results struct through an out
parameter, so both are records and only one of them is a shape this
repo already had.

`GCResults` is `@produced`: libstore made it and Python reads it, like
every other record here.

`GCOptions` is the other direction, and it is the first of its kind. A
caller BUILDS one and hands it down. That is a class rather than four
keyword parameters on the method, because a struct that names a
concept Python has no word for is a class - and flattening it would
make this repo restate four defaults that upstream already states
(tasks/074).
"""

from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.words import GCAction
from huggorm_dsl.declare import (
    U64,
    Bint,
    Cxx,
    Str,
    binding,
    header,
    needs,
    produced,
    reads,
    wire_value,
)


@header("nix/store/gc-store.hh")
@needs("limits")
@binding(
    cxx="nix::GCOptions",
    # Four members the object owns. Nothing here talks to a store.
    threading="pool",
    blocking=False,
)
@wire_value()
class GCOptions:
    """What to collect, and how far to go.

    The whole of `nix-store --gc`'s argument surface in one object.
    `action` decides whether anything is deleted at all, and the other
    three narrow it.

    Built by a caller, which is what makes it the first of its kind
    here. Every other record in this package is `@produced` - something
    libstore made and Python reads.
    """

    def __init__(self, action: GCAction = GCAction.DELETE_DEAD,
                 ignore_liveness: Bint = False,
                 paths_to_delete: list[StorePath] = None,  # noqa: RUF013
                 max_freed: U64 | None = None) -> None:
        """Say what to collect. Every answer has an upstream default.

        `max_freed` is None rather than a number, and that is not a
        convenience: upstream's default is
        `std::numeric_limits<uint64_t>::max()`, which has no Python
        literal a declaration could carry. None leaves the member
        alone, so the default stays the one the struct declares and
        this file does not restate it.
        """
        Cxx("""
new (self) nix::GCOptions{};
self->action = action;
self->ignoreLiveness = ignore_liveness;
self->pathsToDelete = as_set<nix::StorePathSet>(paths_to_delete);
if (max_freed)
    self->maxFreed = *max_freed;
        """)

    @reads("action")
    def action(self) -> GCAction:
        """Which of the four operations to run."""

    @reads("ignoreLiveness")
    def ignore_liveness(self) -> Bint:
        """Whether to delete paths the roots still reach.

        Dangerous, in upstream's own word. A path is still refused
        when another store path depends on it, so this drops the ROOT
        half of the check and keeps the reference half."""

    @reads("pathsToDelete")
    def paths_to_delete(self) -> list[StorePath]:
        """The paths `DELETE_SPECIFIC` should try to delete.

        Read by no other action. Sorted, because Nix keeps them in a
        set and the order is that set's."""

    def max_freed(self) -> U64 | None:
        """Stop once this many bytes have been freed, or None for no
        limit.

        Upstream spells "no limit" as the largest u64, which is a
        SENTINEL rather than a size - no store holds 18 exabytes - so
        None is the honest reading and it is what crosses. Exactly the
        shape `PathInfo.registration_time` has, where upstream spells
        "unknown" as 0.

        This was written the other way first, reading the member back
        as the number, and the wire refused it:

            ValueError: Value out of range: 18446744073709551615

        Every wire field spelled `int` is one proto type and that type
        is signed, so `GCOptions()` - the DEFAULT - could not cross an
        RPC at all. Carrying the absence instead fixes it at the
        declaration, where the sentinel is already a lie. The width
        question underneath is real and is not this: `tasks/079`."""
        Cxx("""
if (self.maxFreed == std::numeric_limits<std::uint64_t>::max())
    return std::nullopt;
return self.maxFreed;
        """)


@header("nix/store/gc-store.hh")
@binding(
    cxx="nix::GCResults",
    threading="pool",
    blocking=False,
)
@produced(by="Store.collect_garbage")
@wire_value()
class GCResults:
    """What a collection found, or removed.

    Both fields depend on the action, so neither means anything
    without it. That is why they arrive together rather than as a
    method's return value.
    """

    @reads("paths", collection="nix::StringSet")
    def paths(self) -> list[Str]:
        """The roots, or the paths that were or would be deleted.

        Strings, not store paths, and that is upstream's own choice:
        the field is a `StringSet`. For `RETURN_LIVE` and
        `RETURN_DEAD` these are store paths spelled out; for the two
        deleting actions they are what was removed. A root can be a
        path outside the store, so the type cannot promise more than
        a string."""

    @reads("bytesFreed")
    def bytes_freed(self) -> U64:
        """How many bytes the two deleting actions freed.

        Zero for `RETURN_LIVE` and `RETURN_DEAD`, which delete
        nothing."""
