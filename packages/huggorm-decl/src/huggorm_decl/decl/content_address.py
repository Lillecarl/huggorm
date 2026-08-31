"""
nix::ContentAddress: `nix/store/content-address.hh`.

A MESSAGE rather than a string, for the reason `decl/hash.py` gives.
`fixed:r:sha256:1abc...` is one rendering of two facts - how the bytes
were serialised, and what that serialisation hashed to - and a caller
who wants either should not be splitting a string on colons to get it.

Nesting, and that is the point of doing this at all. The hash inside
is a `Hash`, which is itself two facts; a wire that carried the
rendered string carried three facts glued together and made every
reader take them apart again.

The words of `ContentAddressMethod` are in `decl/words.py`, with the
other vocabularies. They have no C++ behind them, so they have no
extension to live in.
"""

from huggorm_decl.decl.hash import Hash
from huggorm_decl.decl.words import ContentAddressMethod
from huggorm_dsl.declare import (
    Cxx,
    Str,
    binding,
    header,
    instant,
    local,
    reads,
    wire_value,
)


@header("nix/store/content-address.hh")
@binding(
    cxx="nix::ContentAddress",
    # Two members the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::ContentAddress defaults both operators upstream.
    compare="cxx",
    order=True,
    text="render",
)
class ContentAddress:
    """How a store object addresses itself by its own contents.

    A path with one of these is named after what it holds rather than
    after the derivation that made it, which is why it needs no
    signature: anybody can recompute the name from the bytes.
    """

    def __init__(self, method: ContentAddressMethod, hash: Hash) -> None:
        """Build one from the method and the hash it produced."""
        Cxx("new (self) nix::ContentAddress{method, hash};")

    # WIRE ORDER: the two facts, in the order the rendering states them.

    def method(self) -> ContentAddressMethod:
        """How the bytes were serialised before hashing - `nar` for a
        directory, `flat` for a single file's contents."""
        Cxx("return std::string(self.method.render());")

    @reads("hash")
    def hash(self) -> Hash:
        """The hash of that serialisation.

        A `Hash`, so its algorithm is a field rather than a prefix on
        a string. This is the nesting the message shape buys."""

    # --- the rendering, which is derived from both fields above ------

    @local
    @instant
    def render(self) -> Str:
        """`fixed:r:sha256:<hash>`, or `text:sha256:<hash>`.

        What `nix path-info --json` prints, and what a caller compares
        against a string they were given."""
        Cxx("return self.render();")
