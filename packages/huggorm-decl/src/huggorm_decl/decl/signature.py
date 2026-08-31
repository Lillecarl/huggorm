"""
nix::Signature: `nix/util/signature/local-keys.hh`.

A MESSAGE rather than a string, for the reason `decl/hash.py` gives.
`<key-name>:<base64>` is one rendering of two facts - who vouched, and
what they wrote - and a caller who wants to know WHICH key signed a
path should not be splitting a string to find out.
"""

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


@header("nix/util/signature/local-keys.hh")
@binding(
    cxx="nix::Signature",
    # Two strings the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value(
    # nix::Signature defaults operator<=>, so the binding declares the
    # comparisons rather than comparing parts in Python.
    compare="cxx",
    order=True,
    text="to_string",
)
class Signature:
    """Who vouched for a store path, and what they wrote.

    A signature says a path came from somewhere and arrived intact, so
    both halves matter: the key name is what a caller checks against
    their trusted keys, and the bytes are what verifies.
    """

    def __init__(self, key_name: Str, sig: Bytes) -> None:
        """Build one from its parts.

        No validation, because there is nothing here to validate: a
        signature is checked against a public key, which is a question
        for the store rather than for the constructor."""
        Cxx("new (self) nix::Signature{key_name, std::string(sig.c_str(), sig.size())};")

    # WIRE ORDER: the two facts, in the order the rendering states them.

    @reads("keyName")
    def key_name(self) -> Str:
        """Which key signed it - `cache.nixos.org-1`, usually."""

    def sig(self) -> Bytes:
        """The raw signature bytes, decoded.

        Upstream keeps them decoded and renders base64 on the way out,
        so this is the shorter path as well as the honest one."""
        Cxx("return nb::bytes(self.sig.data(), self.sig.size());")

    # --- the rendering, which is derived from both fields above ------

    @local
    @instant
    def to_string(self) -> Str:
        """`<key-name>:<base64>` - what a .narinfo carries and what
        `nix path-info --sigs` prints."""
