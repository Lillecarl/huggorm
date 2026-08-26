"""
The first binding of a REAL Nix type (tasks/015).

nix::StorePath is the smallest thing that proves the whole chain:
pkg-config linkage against libnixstore, a namespaced C++ class, a
constructor that validates and throws, and accessors returning views
into the object's own storage.

It stands beside the mock's StorePath rather than replacing it. The
mock still backs everything else, and a spike that broke the working
surface would prove nothing.
"""

from declare import Bint, Field, Str, StrView, binding, cxx_name, header, wire_value


# 100% C++, by decision: the C API is not feature complete, so it is
# not a fallback for the awkward cases either.
@header("nix/store/path.hh")
@binding(
    # nix::StorePath deletes its default constructor. That costs
    # nothing: the binding reaches it through a pointer that starts
    # NULL and a factory assigns, never by default-constructing.
    cxx="nix::StorePath",
    # pool: nothing here blocks or touches shared state.
    threading="pool",
    # Every method is a substring of a string already in memory, so
    # there is nothing to release the GIL for and no thread to hop to.
    blocking=False,
)
@wire_value(
    # The base name IS the value, so the one field is read by the one
    # accessor that renders it whole.
    fields=(Field("base_name", "str", read="to_string"),),
    # nix::StorePath defaults operator== and operator<=>, so the
    # binding declares them rather than comparing base names in
    # Python: if upstream ever gives a store path a second field, this
    # follows without an edit.
    compare="cxx",
    order=True,
    text="to_string",
)
class StorePath:
    """A real nix::StorePath.

    Constructible, unlike the mock's StorePath, because the real class
    has a public constructor that takes a base name and validates it.
    That is the honest surface: `StorePath("<hash>-<name>")` either
    gives a store path or raises."""

    def __init__(self, base_name: Str) -> None:
        """Raises when the name is not a store path. The message comes
        from libstore, which is the whole point of binding it."""
        ...

    # Every accessor returns a view INTO the object's own string, which
    # is what StrView says. The emitter copies before anything reaches
    # Python: a view outliving its owner is a dangling pointer, not an
    # exception.
    def to_string(self) -> StrView:
        """The full base name, '<hash>-<name>'."""
        ...

    def name(self) -> StrView:
        """The part after the hash."""
        ...

    @cxx_name("hashPart")
    def hash_part(self) -> StrView:
        """The 32-character base-32 hash."""
        ...

    @cxx_name("isDerivation")
    def is_derivation(self) -> Bint:
        """Whether the name ends in '.drv'."""
        ...
