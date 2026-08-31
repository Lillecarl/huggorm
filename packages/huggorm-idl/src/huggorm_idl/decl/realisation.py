"""
nix::DrvOutput and nix::Realisation: `nix/store/realisation.hh`.

What a CA derivation's output turned out to be. A derivation whose
output is content-addressed does not know its own output path until it
has been built, so the store keeps a mapping - and these two types are
the two halves of it: a KEY you can ask with, and an ANSWER.

Two classes in one file because the header declares both and neither
means anything alone. `DrvOutput` is the key: which derivation, and
which of its outputs. `Realisation` is what the store found.
"""

from huggorm_idl.decl.hash import Hash
from huggorm_idl.decl.path import StorePath
from huggorm_idl.decl.signature import Signature
from huggorm_idl.declare import (
    Cxx,
    Str,
    binding,
    header,
    instant,
    local,
    produced,
    reads,
    wire_value,
)


@header("nix/store/realisation.hh")
@binding(
    cxx="nix::DrvOutput",
    # Two members the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::DrvOutput defaults both operators over exactly these two
    # members, so the C++ comparison and the declared parts agree -
    # which is what makes `compare="cxx"` safe here and not on
    # Realisation below.
    compare="cxx",
    order=True,
    text="to_string",
)
class DrvOutput:
    """Which output of which derivation - the KEY of a realisation.

    Not a store path. A derivation is named here by its "hash modulo",
    which is what lets two derivations that differ only in something
    irrelevant share an output: the hash is computed from the
    derivation for most kinds, and from the fixed content address for
    a fixed-output one.

    Constructible, because a caller builds one to ASK. That is the
    whole of its job.
    """

    def __init__(self, drv_hash: Hash, output_name: Str) -> None:
        """Name an output of a derivation, by the derivation's hash
        modulo and the output's name."""
        Cxx("new (self) nix::DrvOutput{drv_hash, output_name};")

    # WIRE ORDER: the two facts, in the order the rendering states them.

    @reads("drvHash")
    def drv_hash(self) -> Hash:
        """The derivation's hash modulo.

        A `Hash`, so its algorithm is a field rather than a prefix on
        a string."""

    @reads("outputName")
    def output_name(self) -> Str:
        """Which output - `out`, `dev`, `man`."""

    @local
    @instant
    def to_string(self) -> Str:
        """`<hash>!<output-name>`, upstream's own spelling.

        The hash is base16 WITH its algorithm, which is what
        `DrvOutput::parse` reads back."""
        Cxx("return self.to_string();")


@produced(by="Store.query_realisation")
@header("nix/store/realisation.hh")
@binding(
    cxx="nix::Realisation",
    # Every accessor reads memory the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # PARTS, not `compare="cxx"`, and this one is not a preference.
    #
    # nix::UnkeyedRealisation compares on `outPath` ALONE - upstream
    # writes `GENERATE_CMP(UnkeyedRealisation, me->outPath)` with the
    # comment "TODO sketchy that it avoids signatures" - and
    # Realisation's defaulted operators inherit that, so two of these
    # differing only in who signed them are equal in C++.
    #
    # The binding cannot follow that, because `__hash__` is derived
    # from the declared PARTS and signatures are one. Two objects that
    # compared equal would hash differently, which breaks the one
    # invariant Python asks of a value. So equality is over the parts,
    # and it disagrees with upstream about a case upstream calls
    # sketchy.
    compare="parts",
)
class Realisation:
    """What a CA derivation's output turned out to be.

    A VALUE: what the store's database said when it was asked.

    Produced, never constructed. `Store.register_drv_output` is what
    would earn a constructor and it is not bound, so nothing here
    pretends a caller can build one.

    UPSTREAM keeps two types where this keeps one.
    `queryRealisation` answers an `UnkeyedRealisation` - the key is
    redundant, because you just passed it - and `nix::Realisation` is
    that plus the key. The binding hands back the KEYED one, so a
    value that crosses a wire or sits in a list still knows what it is
    the realisation OF. Re-attaching the key is not an invention: it
    is the key the caller asked with.
    """

    # WIRE ORDER: what it is, what it is, and who says so.

    @reads("id")
    def id(self) -> DrvOutput:
        """Which output of which derivation this realises."""

    @reads("outPath")
    def out_path(self) -> StorePath:
        """The store path that output turned out to be.

        This is the whole point of the type. A CA derivation cannot
        know its own output path before it is built, so the store
        remembers."""

    def signatures(self) -> "list[Signature]":
        """Who vouched for this realisation.

        A realisation is a claim that building a derivation produced a
        particular path, and a signature is somebody standing behind
        that claim - which is what lets one machine trust another's
        CA build without repeating it.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""
        Cxx("return as_list(self.signatures);")

    def _from_parts() -> "Realisation":
        """Rebuild one from the parts that crossed.

        An aggregate, unlike PathInfo's: `nix::Realisation` derives
        from `nix::UnkeyedRealisation` publicly and neither declares a
        constructor, so the inner braces initialise the base and the
        last member is the key.

        No parsing. Every part arrives as itself - a StorePath, a
        DrvOutput, a list of Signature - because all three are their
        own messages (tasks/057). This body would have had three
        parses a year ago and has none."""
        Cxx("""
return nix::Realisation{
    {out_path, as_set<std::set<nix::Signature>>(signatures)}, id};
        """)
