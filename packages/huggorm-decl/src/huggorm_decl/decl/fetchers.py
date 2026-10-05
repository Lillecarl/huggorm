"""
nix::fetchers::Input: `nix/fetchers/fetchers.hh`.

What a fetcher fetches, before anything is fetched: `github:NixOS/nixpkgs`
or `{ type = "path"; path = "/src"; }`. A flake reference is one of these
plus the flake's directory inside it.

A VALUE, as `FlakeRef` is and for its reason: Nix 2.34's `Input` keeps no
pointer to the settings it was parsed with, so it outlives them and crosses
as a copy of its attributes.
"""

from huggorm_decl.decl.registry import Attr
from huggorm_decl.decl.store import Store
from huggorm_dsl.declare import (
    NIX_2_36,
    Cxx,
    Str,
    binding,
    blocks,
    header,
    local,
    needs,
    produced,
    threading,
    wire_value,
)


@produced
@header("nix/fetchers/fetchers.hh")
@binding(
    cxx="nix::fetchers::Input",
    threading="pool",
    blocking=False,
)
@wire_value(
    fields=("to_attrs",),
    compare="cxx",
    text="to_string",
)
class Input:
    """A fetcher input, parsed.

    Produced, not constructed, as `FlakeRef` is: a constructor takes the
    parts that cross the wire, and a URL is not one of them."""

    @local
    def to_string(self) -> Str:
        """The input as a URL, as `nix flake metadata` prints it."""

    @local
    def to_url_string(self) -> Str:
        """The input as a URL, with every attribute a query parameter
        where the scheme has no place of its own for it."""
        Cxx("return self.toURLString();")

    def to_attrs(self) -> dict[str, Attr]:
        """The input's attributes, what `builtins.fetchTree` takes."""
        Cxx("return self.toAttrs();")

    @local
    @blocks
    def fingerprint(self, store: Store) -> Str | None:
        """What names this input's contents in the evaluation cache, or
        None for an input that cannot say without fetching.

        BLOCKS: a git work tree with uncommitted changes is answered
        by reading and hashing every changed file."""
        Cxx("return self.getFingerprint(store);")

    @staticmethod
    @needs("huggorm_decl/cpp/call_settings.hpp",
           "nix/fetchers/fetch-settings.hh")
    def _from_parts() -> Input:
        """Rebuild one from its attributes, with the process's fetcher
        settings, as `FlakeRef` does."""
        if NIX_2_36:
            Cxx("return nix::fetchers::Input::fromAttrs(std::move(to_attrs));")
        else:
            Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>({});
return nix::fetchers::Input::fromAttrs(*fetch, std::move(to_attrs));
            """)


@needs("huggorm_decl/cpp/call_settings.hpp", "nix/fetchers/fetch-settings.hh",
       "nix/fetchers/fetchers.hh")
@threading("pool")
def input_from_url(url: Str) -> Input:
    """Parse `url` as `builtins.fetchTree` parses a URL.

    No fetcher setting takes part: Nix 2.36 parses without one, and 2.34
    and 2.35 get the process's."""
    if NIX_2_36:
        Cxx("return nix::fetchers::Input::fromURL(url);")
    else:
        Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>({});
return nix::fetchers::Input::fromURL(*fetch, url);
        """)


@needs("huggorm_decl/cpp/call_settings.hpp", "nix/fetchers/fetch-settings.hh",
       "nix/fetchers/fetchers.hh")
@threading("pool")
def input_from_attrs(attrs: dict[str, Attr]) -> Input:
    """Build an input from its attributes, as `builtins.fetchTree` does
    from an attribute set. Refuses an attribute the input's scheme
    does not take. No fetcher setting takes part, as in
    `input_from_url`."""
    if NIX_2_36:
        Cxx("return nix::fetchers::Input::fromAttrs(nix::fetchers::Attrs(attrs));")
    else:
        Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>({});
return nix::fetchers::Input::fromAttrs(*fetch, nix::fetchers::Attrs(attrs));
        """)
