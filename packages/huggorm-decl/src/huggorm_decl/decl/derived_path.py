"""
nix::DerivedPath and its parts: `nix/store/derived-path.hh`.

What to BUILD, said as an expression rather than as an answer. A
derived path is either a store path you already have, or a derivation
plus which of its outputs you want - and the second arm can nest,
because a derivation's path may itself be another derivation's output.

The first SUM types this repo carries. `tasks/059` has the reasoning;
the short of it is that upstream's own two encodings both tag a union
by shape - a JSON string means opaque, `["*"]` means all outputs - and
protobuf has a real tagged union, so the wire here is better than
either.

There is no `DerivedPathOpaque` on this surface. Upstream's is a
struct holding one `StorePath` and nothing else, so the opaque arm IS
a StorePath and a caller never learns a wrapper existed. Each union
says so on its own alias, and the emitter writes the conversion both
ways from that (tasks/063).

Nothing here prints itself. `DerivedPath::to_string` takes a
`StoreDirConfig &` by upstream's own signature, so rendering is
`store.print_derived_path(dp)` - the same split `print_store_path`
already makes for a StorePath (tasks/040, tasks/042).
"""

from typing import Annotated

from huggorm_decl.decl.path import StorePath
from huggorm_dsl.declare import (
    Bint,
    Cxx,
    Str,
    Variant,
    Wrap,
    binding,
    header,
    reads,
    wire_value,
)


@header("nix/store/outputs-spec.hh")
@binding(
    cxx="nix::OutputsSpec",
    # Two members the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::OutputsSpec defaults operator== over its variant.
    compare="cxx",
)
class OutputsSpec:
    """Which outputs of a derivation are wanted: all, or these.

    A sum type upstream - `variant<All, Names>` - and two fields here,
    because one arm carries nothing and protobuf spells that `bool`.
    The pair is checked at construction rather than trusted: `all` with
    names, and neither, are both refused.

    `all` is NOT "no names". Absence of a container means EMPTY
    everywhere else in this API (tasks/041), and "no outputs" is the
    opposite request from "every output" - so the two are different
    fields and the constructor will not let them blur.
    """

    def __init__(self, all: Bint = False,
                 names: "list[Str]" = None) -> None:  # noqa: RUF013
        """`OutputsSpec(names=["out", "dev"])` names them.
        `OutputsSpec(all=True)` is every output.

        `all` defaults to False so the common case says one thing
        rather than two: naming outputs already says the arm it is
        taking, and `all=False` beside them would restate it.

        Raises when the two disagree. Upstream deletes the default
        constructor to force the choice and asserts that `Names` is
        non-empty; this refuses the same two states in libstore's own
        error type."""
        Cxx("""
if (all) {
    if (!names.empty())
        throw nix::UsageError(
            "outputs: 'all' names nothing, so pass no names");
    new (self) nix::OutputsSpec{nix::OutputsSpec::All{}};
} else {
    if (names.empty())
        throw nix::UsageError(
            "outputs: name at least one output, or ask for all");
    new (self) nix::OutputsSpec{nix::OutputsSpec::Names{
        std::set<std::string, std::less<>>(names.begin(), names.end())}};
}
        """)

    # WIRE ORDER: the tag, then what it names.

    def all(self) -> Bint:
        """Whether every output is wanted, however many there are."""
        Cxx("return std::holds_alternative<nix::OutputsSpec::All>(self.raw);")

    def names(self) -> "list[Str]":
        """The outputs named, or empty when `all` is true.

        Sorted, because upstream keeps them in a set and the order is
        that set's."""
        Cxx("""
if (auto * named = std::get_if<nix::OutputsSpec::Names>(&self.raw))
    return {named->begin(), named->end()};
return {};
        """)


@header("nix/store/derived-path.hh")
@binding(
    cxx="nix::SingleDerivedPathBuilt",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class SingleDerivedPathBuilt:
    """ONE output of a derivation.

    The recursive arm: `drv_path` is itself a `SingleDerivedPath`, so
    this can name the output of a derivation that is itself the output
    of another. That is dynamic derivations, and it is why the wire
    needs a message that nests inside itself rather than a string.
    """

    def __init__(self, drv_path: "SingleDerivedPath", output: Str) -> None:
        """Name one output of one derivation."""
        Cxx("""
new (self) nix::SingleDerivedPathBuilt{
    huggorm::held(drv_path), output};
        """)

    def drv_path(self) -> "SingleDerivedPath":
        """The derivation, which may itself be an output."""
        Cxx("return huggorm::as_arms(*self.drvPath);")

    @reads("output")
    def output(self) -> Str:
        """Which output - `out`, `dev`, `man`."""


SingleDerivedPath = Annotated[
    StorePath | SingleDerivedPathBuilt,
    Variant(
        "nix::SingleDerivedPath",
        # Upstream's union PUBLICLY INHERITS its std::variant and
        # re-exposes it through this. NOT needed to compile: dropping
        # it emits `&p`, and `std::get_if` deduces the variant from
        # the derived class anyway. Stated because it is upstream's
        # own accessor - `&p` leans on the inheritance being public,
        # which is a detail of the header rather than an API of it.
        raw="raw()",
        header="nix/store/derived-path.hh",
        # Upstream's opaque arm is `DerivedPathOpaque`, whose only
        # member is the StorePath Python is given. Both unions
        # alias the SAME struct, and each says so for itself: a
        # union that borrowed the fact from its neighbour would
        # stop being readable on its own.
        wraps={"StorePath": Wrap("nix::DerivedPathOpaque",
                                 holds="path")},
    ),
]
"""A path, or ONE output of a derivation.

The recursion lives here: a `SingleDerivedPathBuilt` holds one of
these, so the arm that is not a plain store path can be another
derivation's output, and so on down.
"""


@header("nix/store/derived-path.hh")
@binding(
    cxx="nix::DerivedPathBuilt",
    threading="pool",
    blocking=False,
)
@wire_value(compare="cxx")
class DerivedPathBuilt:
    """SOME outputs of a derivation.

    The plural of `SingleDerivedPathBuilt`, and upstream keeps both
    because they are different requests: a reference to one output,
    against an ask for a set of them. Upstream's own comment calls the
    plural "sort of icky" - an expression evaluating to several values
    - and keeps it because building is what wants it.
    """

    def __init__(self, drv_path: "SingleDerivedPath",
                 outputs: OutputsSpec) -> None:
        """Ask for some outputs of one derivation."""
        Cxx("""
new (self) nix::DerivedPathBuilt{huggorm::held(drv_path), outputs};
        """)

    def drv_path(self) -> "SingleDerivedPath":
        """The derivation, which may itself be an output."""
        Cxx("return huggorm::as_arms(*self.drvPath);")

    @reads("outputs")
    def outputs(self) -> OutputsSpec:
        """Which of its outputs are wanted."""


DerivedPath = Annotated[
    StorePath | DerivedPathBuilt,
    Variant(
        "nix::DerivedPath",
        raw="raw()",
        header="nix/store/derived-path.hh",
        # Upstream's opaque arm is `DerivedPathOpaque`, whose only
        # member is the StorePath Python is given. Both unions
        # alias the SAME struct, and each says so for itself: a
        # union that borrowed the fact from its neighbour would
        # stop being readable on its own.
        wraps={"StorePath": Wrap("nix::DerivedPathOpaque",
                                 holds="path")},
    ),
]
"""A path to fetch, or outputs to build.

What `Store.query_missing` and `Store.build_paths` take. The opaque
arm is a plain StorePath, because upstream's wrapper around one holds
nothing else.
"""
