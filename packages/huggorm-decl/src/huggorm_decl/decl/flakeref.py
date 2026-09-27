"""
nix::FlakeRef: `nix/flake/flakeref.hh`.

What `github:NixOS/nixpkgs/nixos-unstable` or `./.` means before
anything is fetched: an input for a fetcher, and the subdirectory of
the flake inside it.

A VALUE. Nix 2.34's `Input` keeps no pointer to the settings it was
parsed with, so a reference outlives them and crosses as a copy. Its
one part is its attributes: `toAttrs` writes the subdirectory as
`dir`, and `fromAttrs` reads it back.
"""

from huggorm_decl.decl.registry import Attr
from huggorm_dsl.declare import (
    Cxx,
    Str,
    binding,
    blocks,
    header,
    local,
    needs,
    produced,
    reads,
    threading,
    wire_value,
)


@produced(by="parse_flake_ref")
@header("nix/flake/flakeref.hh")
@binding(
    cxx="nix::FlakeRef",
    # Every accessor reads what the object holds.
    threading="pool",
    blocking=False,
)
@wire_value(
    fields=("to_attrs",),
    # `operator==` is defaulted upstream. There is an `operator<` and
    # no `<=>`, so the binding declares no ordering.
    compare="cxx",
    text="to_string",
)
class FlakeRef:
    """A flake reference, parsed.

    Produced, not constructed: a constructor takes the parts that
    cross the wire, and a URL is not one of them."""

    @local
    def to_string(self) -> Str:
        """The reference as a URL, as `nix flake metadata` prints it."""

    @local
    @reads("subdir")
    def subdir(self) -> Str:
        """The flake's directory inside the input, empty at the top."""

    def to_attrs(self) -> dict[str, Attr]:
        """The input's attributes, with the subdirectory as `dir`."""
        Cxx("return self.toAttrs();")

    @needs("huggorm_decl/cpp/call_settings.hpp",
           "nix/fetchers/fetch-settings.hh")
    def _from_parts() -> FlakeRef:
        """Rebuild one from its attributes, with the process's fetcher
        settings: the attributes already say everything a setting would
        decide at parse time."""
        Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>({});
return nix::FlakeRef::fromAttrs(*fetch, to_attrs);
        """)


@needs("huggorm_decl/cpp/call_settings.hpp", "nix/fetchers/fetch-settings.hh",
       "nix/flake/flakeref.hh")
@threading("pool")
@blocks
def parse_flake_ref(url: Str, base: Str | None = None,
                    settings: dict[str, Str] | None = None) -> FlakeRef:
    """Parse `url` as `nix` parses a flake reference.

    `base` is the directory a relative path names from, None for the
    working directory. Without one a git working tree parses as `path:`
    rather than `git+file:`, and `./x` does not parse at all. A path
    reference looks for `.git` upward, so this reads the filesystem.

    `settings` are fetcher settings over the process's. Nix requires the
    `flakes` feature, and so does this."""
    Cxx("""
auto fetch = huggorm::call_settings<nix::fetchers::Settings>(
    settings.value_or(std::map<std::string, std::string>{}));
auto dir = base ? std::filesystem::path(*base) : std::filesystem::current_path();
return nix::parseFlakeRef(*fetch, url, dir);
    """)
