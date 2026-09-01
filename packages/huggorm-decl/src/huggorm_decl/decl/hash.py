"""
nix::Hash, and everything about it: `nix/util/hash.hh`.

A MESSAGE rather than a string. `sha256:1abc...` is one rendering of
two facts - which algorithm, and which digest - and a wire that
carries the rendering makes every reader parse it back to learn
either. So the two facts cross, and the rendering stays available as
what it is: a way to print one.

The words of `HashAlgorithm` are in `decl/words.py`, with the other
vocabularies. `nix::HashAlgorithm` is behind them and the declaration
says so, which is what lets `algorithm` read a member instead of
converting - but a StrEnum compiles to nothing, so it has no
extension of its own to live in.
"""

from huggorm_decl.decl.words import HashAlgorithm
from huggorm_dsl.declare import (
    Bytes,
    Cxx,
    Str,
    binding,
    header,
    instant,
    local,
    reads,
    wire_value,
)


@header("nix/util/hash.hh")
@binding(
    cxx="nix::Hash",
    # Every accessor reads memory the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::Hash defaults operator== and operator<=> upstream, so the
    # binding declares them rather than comparing the parts in Python.
    # A hash is the thing you compare, and two of them are equal when
    # the same algorithm gave the same bytes.
    compare="cxx",
    order=True,
    # `str(h)` is `sha256:<base32>`, which is what a hash looks like
    # everywhere Nix prints one.
    text="to_string",
)
class Hash:
    """A digest, and the algorithm that produced it.

    Both halves, because neither is the whole answer: a digest without
    its algorithm cannot be checked, and `nix::Hash` carries the pair
    for exactly that reason.

    Constructible from its parts, which is what a caller has when they
    have read a hash from somewhere Nix did not print it.
    """

    def __init__(self, algorithm: HashAlgorithm, digest: Bytes) -> None:
        """Raises when the digest is the wrong length for the
        algorithm. A sha256 is 32 bytes and nothing else is."""
        Cxx("""
new (self) nix::Hash(algorithm);
if (digest.size() != self->hashSize)
    throw nix::BadHash(
        "a %s digest is %d bytes, not %d",
        nix::printHashAlgo(algorithm), self->hashSize, digest.size());
// `c_str`, not `digest[i]`: indexing an nb::bytes hands back an
// accessor for Python's sake, and this wants the bytes.
const char * given = digest.c_str();
for (std::size_t i = 0; i < self->hashSize; ++i)
    self->hash[i] = static_cast<std::uint8_t>(given[i]);
        """)

    # WIRE ORDER: the two facts, in the order a caller says them.

    @reads("algo")
    def algorithm(self) -> HashAlgorithm:
        """Which digest this is - `sha256` for almost everything."""

    def digest(self) -> Bytes:
        """The raw digest. Not a rendering of it.

        32 bytes for a sha256. This is what the wire carries, because
        it is what the hash IS - every printed form is these bytes in
        some alphabet, and a caller who wants one asks for it by
        name."""
        Cxx("""
return nb::bytes(
    reinterpret_cast<const char *>(self.hash), self.hashSize);
        """)

    # --- renderings: a way to print one, not a way to hold one -------
    #
    # `@local`, every one. They are derived from the two fields above,
    # so sending them would be the same bytes a fourth, fifth and
    # sixth time - and `_from_parts` could rebuild the hash without
    # any of them, which is the test.

    # Pure string work over bytes already in memory. Releasing the GIL
    # would cost two thread-state transitions to save nothing.
    @local
    @instant
    def to_string(self) -> Str:
        """`sha256:<base32>` - what `nix path-info` prints, and what
        a .narinfo carries.

        The algorithm travels with the digest, so a reader is never
        told separately which one it is."""
        Cxx("""
return self.to_string(nix::HashFormat::Nix32, /*includeAlgo=*/true);
        """)

    @local
    @instant
    def base16(self) -> Str:
        """The digest as lowercase hex, with no algorithm in front.

        What a `.drv` carries and what `nix hash` prints with
        `--base16`. Bare, because the algorithm is a field of this
        message and repeating it inside a rendering is the coupling
        this type exists to undo."""
        Cxx("""
return self.to_string(nix::HashFormat::Base16, /*includeAlgo=*/false);
        """)

    @local
    @instant
    def sri(self) -> Str:
        """`sha256-<base64>`, the Subresource Integrity spelling.

        What a flake lock file and a fetcher argument use."""
        Cxx("""
return self.to_string(nix::HashFormat::SRI, /*includeAlgo=*/true);
        """)
