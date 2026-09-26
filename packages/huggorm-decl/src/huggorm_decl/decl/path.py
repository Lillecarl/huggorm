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

from huggorm_dsl.declare import (
    Bint,
    Field,
    Str,
    StrView,
    binding,
    binds,
    cxx_name,
    header,
    needs,
    startup,
    translator,
    wire_value,
)


# 100% C++, by decision: the C API is not feature complete, so it is
# not a fallback for the awkward cases either.
@header("nix/store/path.hh")
@binding(
    # nix::StorePath deletes its default constructor. That costs
    # nothing: the binding reaches it through a pointer that starts
    # NULL and a factory assigns, never by default-constructing.
    cxx="nix::StorePath",
    # libstore holds a set of these, not a vector - queryValidPaths,
    # computeFSClosure and addToStore all take StorePathSet. The wire
    # carries a list, so every `list[StorePath]` parameter converts,
    # and saying it here is what stops a dozen call sites saying it.
    collection="nix::StorePathSet",
    # pool: nothing here blocks or touches shared state.
    threading="pool",
    # Every method is a substring of a string already in memory, so
    # there is nothing to release the GIL for and no thread to hop to.
    blocking=False,
)
@wire_value(
    # The base name IS the value, so the one field is read by the one
    # accessor that renders it whole.
    fields=(Field("base_name", read="to_string"),),
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

    # `base_name`, because nix/store/path.hh:45 declares
    # `StorePath(std::string_view baseName)` and the rule is to follow
    # Nix unless there is a reason not to. nanopynix calls this
    # parameter `path`, which also reads badly beside the class's own
    # `name()` accessor - `name` is the part after the hash, and a
    # base name is the whole of it.
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


# --- what the module does before a caller exists -------------------

# Neither of these is surface. They are declared because this is
# where a module's C++ facts live, and a module that does not bind
# libstore needs neither - which is what the emitter used to assume
# and get wrong.


@needs("huggorm_decl/cpp/libstore.hpp")
@binds("huggorm::init_libstore")
@startup
def _init_libstore() -> None:
    """Initialise libstore, once, at import.

    libstore does not raise when it has not been initialised: it
    ABORTS the process, with "The program must call nix::initNix()
    before calling any libstore library functions". A binding cannot
    let a caller discover that, so this runs before anything else in
    the module - including the imports, which run another module's
    initialisation.
    """


@needs("huggorm_decl/cpp/errors.hpp")
@binds("huggorm::translate_nix_error")
@translator
def _translate_nix_error() -> None:
    """Map a nix exception onto the right class in errors.py.

    nix has an exception hierarchy worth keeping - BadStorePath is a
    different answer from InvalidPath - and nanobind's default would
    flatten every one of them to RuntimeError.
    """
