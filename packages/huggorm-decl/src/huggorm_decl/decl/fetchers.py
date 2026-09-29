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


@produced(by="input_from_url")
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

        BLOCKS: a path input hashes its tree to answer."""
        Cxx("return self.getFingerprint(store);")

    @needs("huggorm_decl/cpp/call_settings.hpp",
           "nix/fetchers/fetch-settings.hh")
    def _from_parts() -> Input:
        """Rebuild one from its attributes, with the process's fetcher
        settings, as `FlakeRef` does."""
        Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>({});
return nix::fetchers::Input::fromAttrs(*fetch, std::move(to_attrs));
        """)


@needs("huggorm_decl/cpp/call_settings.hpp", "nix/fetchers/fetch-settings.hh",
       "nix/fetchers/fetchers.hh")
@threading("pool")
def input_from_url(url: Str, settings: dict[str, Str] | None = None) -> Input:
    """Parse `url` as `builtins.fetchTree` parses a URL.

    `settings` are fetcher settings over the process's."""
    Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>(
    settings.value_or(std::map<std::string, std::string>{}));
return nix::fetchers::Input::fromURL(*fetch, url);
    """)


@needs("huggorm_decl/cpp/call_settings.hpp", "nix/fetchers/fetch-settings.hh",
       "nix/fetchers/fetchers.hh")
@threading("pool")
def input_from_attrs(attrs: dict[str, Attr],
                     settings: dict[str, Str] | None = None) -> Input:
    """Build an input from its attributes, as `builtins.fetchTree` does
    from an attribute set. Refuses an attribute the input's scheme
    does not take."""
    Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>(
    settings.value_or(std::map<std::string, std::string>{}));
return nix::fetchers::Input::fromAttrs(*fetch, nix::fetchers::Attrs(attrs));
    """)
