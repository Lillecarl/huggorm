"""
The mock store: the exemplars every wire and threading rule was
written against.

fake-library is a C++ stand-in this repo grew before real Nix was
linked. Each class carries a Mock prefix from the moment its real
counterpart lands and takes the plain name, so the prefix is a map of
what is left to do.

It is still where the awkward shapes live, and that is why it is
worth declaring rather than deleting. An ABSTRACT base that Python
subclasses and C++ calls back into. A value that looks like data and
must travel as a proxy because reading it mutates a counter. A value
whose field names another value, so its message nests. Nothing on the
real Nix side has needed any of those yet.
"""

from cythonix_idl.declare import (
    I64,
    Bint,
    Field,
    Str,
    abstract,
    binding,
    binds,
    cxx_body,
    cxx_name,
    derives,
    header,
    produced,
    pure,
    threading,
    wire_value,
)


@produced(by="a store")
@header("fake_library/store.hpp")
@binding(
    cxx="fake_library::StorePath",
    # Nothing here allocates, does IO or waits: every accessor reads a
    # substring of the one string this object holds. So there is no
    # thread to hop to and no GIL to release, and the codegen emits no
    # async wrapper - a MockStorePath is handed back as itself, on
    # both sides of the wire.
    threading="pool",
    blocking=False,
)
@wire_value(
    # The serialization contract, and the field list IS the proto
    # message shape. A field type naming another wire value nests that
    # type's message.
    fields=(Field("base_name", "str", read="to_string"),),
    text="to_string",
)
class MockStorePath:
    """An immutable store path: safe to serialize, so it crosses
    wrapper boundaries as a copy."""

    def to_string(self) -> Str:
        """The full base name, '<hash>-<name>'."""

    @cxx_name("hash")
    def hash_part(self) -> Str:
        """The 32-character hash at the front."""

    @cxx_name("name")
    def name_part(self) -> Str:
        """The part after the hash."""


@produced(by="Store.query_derivation")
@header("fake_library/store.hpp")
@binding(
    cxx="fake_library::Derivation",
    # `describe` bumps an access counter, so two threads reading one
    # derivation race. Affine.
    threading="affine",
)
class MockDerivation:
    """The instructive wire case: it looks like a value, and it is
    not.

    `set_env` and the access counter mutate it, so despite being a
    plain data holder it must travel as a PROXY. Mutability forces
    proxy, always."""

    def set_env(self, key: Str, value: Str) -> None:
        """Set one environment variable of the derivation."""

    def describe(self) -> Str:
        """A one-line description.

        Mutates an internal counter, which is why the type is
        affine."""

    # A COUNT, and `int queries() const` upstream (store.hpp:64). It
    # was declared Bint, so the stub said bool, the message carried
    # `bool result = 1`, and a count of three crossed the wire as
    # True. Found by diffing nanobind's rendered signature against
    # this file.
    def queries(self) -> I64:
        """How many times `describe` has been called."""


@header("fake_library/store.hpp")
@binding(
    cxx="fake_library::DerivedPath",
    # An immutable build request whose one method formats its own
    # fields. No wrapper.
    threading="pool",
    blocking=False,
)
@wire_value(
    # A wire-value field may name another wire-value type: the emitted
    # message nests MockStorePathMsg and the codec recurses into it. A
    # trailing "?" marks an optional field - proto3 cannot tell an
    # unset string from an empty one, so the contract says which way
    # to read it back. Opaque requests carry no output.
    fields=(Field("path", "MockStorePath", read="path"),
            Field("output", "str?", read="output_name")),
)
class MockDerivedPath:
    """Which store path to build, and which output of it."""

    # `mdp` is the storage `__init__` was handed, which is the
    # emitter's own name for the object - initials of the class, the
    # same rule every other body here follows.
    @cxx_body("""if (output.has_value())
            new (mdp) fake_library::DerivedPath(path, *output);
        else
            new (mdp) fake_library::DerivedPath(path);""")
    def __init__(self, path: "MockStorePath",
                 output: "str | None" = None) -> None:
        """A request for a path, or for one output of a derivation.

        Two C++ constructors behind one Python signature: an opaque
        request carries no output name, and a built one does."""

    def describe(self) -> Str:
        """'opaque <path>' or '<path>!<output>'."""

    def path(self) -> "MockStorePath":
        """The store path this request names."""

    @cxx_body("""if (!mdp.is_built())
            return std::nullopt;
        return mdp.output_name();""")
    def output_name(self) -> "str | None":
        """The output name, or None for an opaque request."""


@header("fake_library/store.hpp")
@abstract
@binding(
    cxx="fake_library::Store",
    # A concrete store registers into a mutex-guarded table, so any
    # pool thread will do.
    threading="pool",
)
class MockStore:
    """The type real callers hold most of the time.

    You ask for a store and use it without caring which
    implementation answered - so it needs an async wrapper and a wire
    identity of its own. What it does not need is CONSTRUCTION: a bare
    MockStore() would be an object with no implementation behind it.
    """

    @pure
    def get_uri(self) -> Str:
        """How this store describes itself.

        The one method a Python subclass may override, and the reason
        this binding has a trampoline: `describe` goes through C++
        virtual dispatch, so an override has to be visible from
        there."""

    def is_valid_path(self, path: "MockStorePath") -> Bint:
        """Whether this store holds that path."""

    def query_all_valid_paths(self) -> "list[MockStorePath]":
        """Every path this store holds.

        A repeated field on the wire. MockStorePath is a wire value,
        so each element crosses as its own message rather than as a
        handle - a list of proxies is refused, because nothing grants
        leases in bulk."""

    def add_text_to_store(self, name: Str, contents: Str) -> "MockStorePath":
        """Add a text file and hand back the path it landed at."""

    def build_derivation(self, request: "MockDerivedPath") -> "MockStorePath":
        """Build one request and hand back the output path."""

    def query_derivation(self, drv_path: "MockStorePath") -> "MockDerivation":
        """Parse a .drv previously added to this store.

        The returned derivation carries mutable state: callers must
        keep invoking it on this store's thread, which the async layer
        enforces."""


@header("fake_library/store.hpp")
@derives("MockStore")
@binding(cxx="fake_library::LocalStore", threading="pool")
class MockLocalStore:
    """A store on this machine. Constructed, unlike its base."""

    def __init__(self) -> None:
        """Open the local store."""


@header("fake_library/store.hpp")
@derives("MockStore")
@binding(
    cxx="fake_library::RemoteStore",
    # A remote store talks over a connection it does not share.
    threading="affine",
)
class MockRemoteStore:
    """A store reached over a connection. Constructed, like its
    sibling."""

    def __init__(self) -> None:
        """Open the remote store."""


@threading("pool")
@binds("fake_library::describe_store")
def describe(obj: "MockStore") -> Str:
    """Describe a store through C++ virtual dispatch.

    The point of the function: it takes the BASE by reference, so it
    sees a `get_uri` override on a Python subclass of MockStore - the
    trampoline is what makes that work - and not on a plain-Python
    override of MockLocalStore or MockRemoteStore, which override the
    Python method and leave the C++ vtable alone.
    """
