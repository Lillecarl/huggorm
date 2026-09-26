"""
nix::StoreReference: `nix/store/store-reference.hh`.

A store URI as libstore reads it, before any store opens: which kind
of store it names, and the query parameters. `parse_store_reference`
makes one, and `Store.reference()` renders an opened store's.

The kind is a SUM type upstream, `variant<Auto, Specified, Daemon,
Local>`, and it crosses as one. `Daemon` and `Local` derive from
`Specified` in C++ with a fixed scheme, and they stay arms of their
own: `daemon` and `unix://` parse to different arms, and a caller that
asks which one it got must be told.
"""

from typing import Annotated

from huggorm_dsl.declare import (
    Bint,
    Cxx,
    Str,
    Variant,
    binding,
    header,
    needs,
    produced,
    reads,
    wire_value,
)


@header("nix/store/store-reference.hh")
@binding(
    cxx="nix::StoreReference::Auto",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx", unit=True)
class StoreReferenceAuto:
    """`auto`: the store the configuration names, chosen when it opens."""

    def __init__(self) -> None:
        """Nothing to say: the arm is the whole fact."""
        Cxx("new (self) nix::StoreReference::Auto{};")


@header("nix/store/store-reference.hh")
@binding(
    cxx="nix::StoreReference::Daemon",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx", unit=True)
class StoreReferenceDaemon:
    """`daemon`: the local daemon's socket, with scheme `unix`."""

    def __init__(self) -> None:
        """Nothing to say: the arm is the whole fact."""
        Cxx("new (self) nix::StoreReference::Daemon{};")


@header("nix/store/store-reference.hh")
@binding(
    cxx="nix::StoreReference::Local",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx", unit=True)
class StoreReferenceLocal:
    """`local`: the store on this machine's disk, with scheme `local`."""

    def __init__(self) -> None:
        """Nothing to say: the arm is the whole fact."""
        Cxx("new (self) nix::StoreReference::Local{};")


@header("nix/store/store-reference.hh")
@binding(
    cxx="nix::StoreReference::Specified",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class StoreReferenceSpecified:
    """A URI with a scheme, such as `ssh-ng://host` or `file:///cache`.

    A bare path parses to this too, with scheme `local` and the path
    as its authority."""

    def __init__(self, scheme: Str, authority: Str = "") -> None:
        """A scheme, and what follows `://`."""
        Cxx("new (self) nix::StoreReference::Specified{scheme, authority};")

    @reads("scheme")
    def scheme(self) -> Str:
        """The part before `://`."""

    @reads("authority")
    def authority(self) -> Str:
        """The part after `://`, as written: Nix does not unescape it."""


StoreReferenceVariant = Annotated[
    StoreReferenceAuto
    | StoreReferenceSpecified
    | StoreReferenceDaemon
    | StoreReferenceLocal,
    Variant(
        "nix::StoreReference::Variant",
        header="nix/store/store-reference.hh",
        # A typedef of `std::variant<Auto, Specified, Daemon, Local>`,
        # the arms above in that order.
        bare=True,
    ),
]
"""Which kind of store a reference names."""


@produced(by="parse_store_reference")
@header("nix/store/store-reference.hh")
@binding(
    cxx="nix::StoreReference",
    threading="pool",
    blocking=False,
)
class StoreReference:
    """A store URI, parsed."""

    @reads("variant")
    def variant(self) -> StoreReferenceVariant:
        """Which kind of store it names."""

    @reads("params")
    def params(self) -> dict[str, Str]:
        """The query parameters, decoded."""

    def render(self, with_params: Bint = True) -> Str:
        """The URI again, as Nix normalises it."""
        Cxx("return self.render(with_params);")


@needs("nix/store/store-reference.hh")
def parse_store_reference(uri: Str) -> StoreReference:
    """Read `uri` as libstore does before it opens a store.

    Raises `UsageError` for a URI libstore cannot read."""
    Cxx("return nix::StoreReference::parse(uri);")
