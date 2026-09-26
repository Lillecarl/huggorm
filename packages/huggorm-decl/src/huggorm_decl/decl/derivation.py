"""
nix::Derivation and its outputs: `nix/store/derivations.hh`.

What a `.drv` file says: a builder, its arguments and environment, the
inputs it needs, and the outputs it makes. `Store.read_derivation`
reads one and `Store.add_derivation` writes one, which is `nix
derivation show` and `nix derivation add`.

An output is a SUM type upstream, `variant<InputAddressed, CAFixed,
CAFloating, Deferred, Impure>`, and it crosses as one: five arms, each
a value, tagged on the wire as `DerivedPath` is (tasks/059).

A derivation is read, not rewritten, here. A caller that wants a
modified one - `nix develop` does - edits `to_json()` and hands the
document to `Store.add_derivation`, which is upstream's own route for
a derivation from outside the evaluator: it fills in the output paths
and checks the invariants.
"""

from typing import Annotated

from huggorm_decl.decl.content_address import ContentAddress
from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.words import ContentAddressMethod, HashAlgorithm
from huggorm_dsl.declare import (
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


@header("nix/store/derivations.hh")
@binding(
    cxx="nix::DerivationOutput::InputAddressed",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class DerivationOutputInputAddressed:
    """An output named after the derivation that makes it."""

    def __init__(self, path: StorePath) -> None:
        """The path the derivation's hash names."""
        Cxx("new (self) nix::DerivationOutput::InputAddressed{path};")

    @reads("path")
    def path(self) -> StorePath:
        """Where the output goes, known before it is built."""


@header("nix/store/derivations.hh")
@binding(
    cxx="nix::DerivationOutput::CAFixed",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class DerivationOutputCAFixed:
    """A fixed-output derivation: the content is known in advance.

    What a fetcher is. The derivation states the hash, so the path is
    known before the build and the build is checked against it."""

    def __init__(self, ca: ContentAddress) -> None:
        """The content address the output must have."""
        Cxx("new (self) nix::DerivationOutput::CAFixed{ca};")

    @reads("ca")
    def ca(self) -> ContentAddress:
        """The method and hash the output must match."""


@header("nix/store/derivations.hh")
@binding(
    cxx="nix::DerivationOutput::CAFloating",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class DerivationOutputCAFloating:
    """A content-addressed output whose hash is known only after the
    build. Needs the `ca-derivations` experimental feature."""

    def __init__(self, method: ContentAddressMethod,
                 hash_algo: HashAlgorithm) -> None:
        """How the finished output will be hashed."""
        Cxx("new (self) nix::DerivationOutput::CAFloating{method, hash_algo};")

    @reads("method")
    def method(self) -> ContentAddressMethod:
        """How the output is serialised before hashing."""

    @reads("hashAlgo")
    def hash_algo(self) -> HashAlgorithm:
        """Which hash names it."""


@header("nix/store/derivations.hh")
@binding(
    cxx="nix::DerivationOutput::Deferred",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx", unit=True)
class DerivationOutputDeferred:
    """An input-addressed output whose path is not known yet, because
    an input is content-addressed and not built. Also what `nix
    develop` makes every output of its shell derivation."""

    def __init__(self) -> None:
        """Nothing to say: the arm is the whole fact."""
        Cxx("new (self) nix::DerivationOutput::Deferred{};")


@header("nix/store/derivations.hh")
@binding(
    cxx="nix::DerivationOutput::Impure",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class DerivationOutputImpure:
    """An output of an impure derivation: content-addressed, and never
    registered as a realisation. Needs `impure-derivations`."""

    def __init__(self, method: ContentAddressMethod,
                 hash_algo: HashAlgorithm) -> None:
        """How the finished output will be hashed."""
        Cxx("new (self) nix::DerivationOutput::Impure{method, hash_algo};")

    @reads("method")
    def method(self) -> ContentAddressMethod:
        """How the output is serialised before hashing."""

    @reads("hashAlgo")
    def hash_algo(self) -> HashAlgorithm:
        """Which hash names it."""


DerivationOutput = Annotated[
    DerivationOutputInputAddressed
    | DerivationOutputCAFixed
    | DerivationOutputCAFloating
    | DerivationOutputDeferred
    | DerivationOutputImpure,
    Variant(
        "nix::DerivationOutput",
        raw="raw",
        header="nix/store/derivations.hh",
    ),
]
"""How one output of a derivation is addressed."""


@produced(by="Store.read_derivation")
@header("nix/store/derivations.hh")
@binding(
    cxx="nix::Derivation",
    threading="pool",
    blocking=False,
)
class Derivation:
    """One `.drv`, as libstore parsed it."""

    @reads("name")
    def name(self) -> Str:
        """The name, without `.drv`."""

    @reads("platform")
    def system(self) -> Str:
        """The system it builds on, such as `x86_64-linux`."""

    @reads("builder")
    def builder(self) -> Str:
        """The program the build runs."""

    @reads("args")
    def args(self) -> list[Str]:
        """The builder's arguments, in order."""

    @reads("env")
    def env(self) -> dict[str, Str]:
        """The builder's environment."""

    @reads("inputSrcs")
    def input_srcs(self) -> list[StorePath]:
        """Store paths the build reads that no derivation makes."""

    @needs("nix/store/store-api.hh")
    def input_drvs(self) -> dict[str, list[Str]]:
        """The derivations the build needs, by `.drv` base name, and
        which of their outputs.

        Keyed by base name because a map is keyed by str on the wire.

        Raises for an output of an output: that is dynamic
        derivations, which this does not carry, and leaving the
        nested outputs out would be a silent answer. `to_json()` has
        them."""
        Cxx("""
std::map<std::string, std::vector<std::string>> out;
for (auto & [path, node] : self.inputDrvs.map) {
    if (!node.childMap.empty())
        throw nix::Unsupported(
            "'%s' needs an output of an output of '%s' (dynamic derivations)",
            self.name, path.to_string());
    out.emplace(std::string(path.to_string()),
                std::vector<std::string>(node.value.begin(), node.value.end()));
}
return out;
        """)

    @reads("outputs")
    def outputs(self) -> dict[str, DerivationOutput]:
        """Each output by name, and how it is addressed."""

    @needs("nlohmann/json.hpp")
    def structured_attrs(self) -> Str | None:
        """The structured attributes as JSON text, or None when the
        derivation passes its attributes as environment variables."""
        Cxx("""
if (!self.structuredAttrs)
    return std::nullopt;
return nlohmann::json(self.structuredAttrs->structuredAttrs).dump();
        """)

    @needs("nlohmann/json.hpp")
    def to_json(self) -> Str:
        """The derivation as Nix's own JSON document.

        One entry of what `nix derivation show` prints, and what
        `Store.add_derivation` takes back."""
        Cxx("return nlohmann::json(self).dump();")
