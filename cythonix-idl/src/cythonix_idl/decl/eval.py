"""
The evaluation binding: EvalState is the affine SERVICE exemplar.

`nix::EvalState` is documented as not thread-safe, one per thread;
here that fact becomes `threading="affine"`. Values live on the
state's thread: they are produced by state methods and attach to its
runner in the async layer. `force` mutates a value in place, which is
the affine-value-as-parameter case.

Two facts about libexpr shape everything below, and both are LIFETIME.

A value lives in the collector's heap and belongs to nobody: it dies
when the collector can no longer see a pointer to it, even while its
EvalState lives on. Python's heap is not scanned, so a wrapper
holding a bare pointer roots nothing.

And a value is not self-describing. An attribute name is a `Symbol`,
a `uint32_t` index into the PRODUCING state's symbol table, so
rendering one needs that state in hand.

`cythonix::Bridge` answers both: it holds an upstream `RootValue` and
a share of the state that made it, so the state cannot die under a
value that still points into its memory. That is producer pinning as
a C++ fact, beside the server's `parents=[self]`. It is the only C++
in this binding a declaration could not have written, and
`_cpp/eval.hpp` says why line by line.
"""

from cythonix_idl.declare import (
    I64,
    Bint,
    Cxx,
    Str,
    binding,
    binds,
    blocks,
    cxx_name,
    header,
    needs,
    produced,
    startup,
    threading,
    tree,
)


@produced(by="EvalState")
@header("cythonix_bindings/_cpp/eval.hpp")
@binding(
    # One state per thread, and its values belong to that thread. The
    # async layer inherits the runner rather than making a new one -
    # a value operation touches the state's own memory, so running it
    # anywhere else is a data race.
    threading="affine",
    # Reading a forced value is a memory read. Forcing is what waits,
    # and it says so for itself.
    blocking=False,
    cxx="cythonix::Bridge",
)
# How a value TREE is walked, read by the RPC layer so that no layer
# above this declaration knows what a Value is or which of its methods
# do what (tasks/030). `kind` names the accessor that says what this
# node is; its answer selects one of the branches below. A kind named
# nowhere here crosses as a proxy - and real Nix has five of those:
# thunk, function, external, failed and path. Laziness the wire cannot
# serialize, plus three things that are not data at all.
@tree(
    kind="type_name",
    # What makes two nodes THE SAME node. A fresh wrapper is built for
    # every access, so Python identity says nothing: two wrappers over
    # one value differ, and a wrapper that dies hands its id() to the
    # next one. The underlying value's address is the identity.
    identity="_identity",
    # kind reported by `kind` -> [wire type, accessor]. The wire type
    # is what picks the arm, so the layer above reads a declared type
    # name rather than a label this file invented.
    scalars={"int": ["int", "integer"],
             "string": ["str", "string_value"],
             "bool": ["bool", "boolean"]},
    list={"size": "size", "item": "at"},
    attrs={"size": "size", "name": "name_at", "value": "value_at"},
)
class Value:
    """One GC-resident nix::Value, rooted for as long as Python holds
    it.

    Wire-proxy despite being "just data": a thunk must force on its
    home thread and forcing mutates in place. A future refinement may
    serialize forced scalars; until then, proxy.

    Produced, never constructed. A value comes from an EvalState -
    parsed, evaluated or built - and there is nothing a caller could
    correctly make one from.

    EVERY ACCESSOR GUARDS, and that is not politeness. `nix::Value` is
    a tagged union whose readers are `noexcept` and undefined on the
    wrong tag: reading `integer` off a string is not an error, it is a
    reinterpretation of the payload. So no method here binds a
    pointer-to-member on `nix::Value`; each one is a Bridge method
    that checks the tag first."""

    @cxx_name("identity")
    def _identity(self) -> I64:
        """The underlying value's address, as a number.

        Private: it is not surface, so the codegen leaves it out of
        every generated form. The tree walk uses it to visit a shared
        value once - values are immutable and shared freely, so
        without it a diamond is copied and a cycle never ends."""

    def is_gc_managed(self) -> Bint:
        """True when this value lives inside a GC-allocated block.

        Bound straight from gc.h: a no-op integration cannot fake
        it."""

    def type_name(self) -> Str:
        """What this value is: "thunk", "int", "float", "bool",
        "string", "path", "null", "attrs", "list", "function",
        "external" or "failed".

        The full `nix::ValueType`, not a subset. A kind the `@tree`
        map above does not name crosses as a proxy, so naming all of
        them here costs nothing and hides nothing."""

    def integer(self) -> I64:
        """This value as an integer. Raises on a thunk, or on a value
        of another kind."""

    def string_value(self) -> Str:
        """This value as a string. Raises as `integer` does.

        The string CONTEXT is dropped. A Nix string can carry store
        paths it depends on, and nothing above this layer can act on
        them yet; a declaration that carried them would be inventing
        a surface rather than binding one."""

    def boolean(self) -> Bint:
        """This value as a bool. Raises as `integer` does."""

    # Collections. Reading is by index, which is also how the
    # alphabetical order of an attribute set reaches Python.
    #
    # That order is not free. nix::Bindings is sorted by Symbol ID,
    # which is INTERNING order - the order a name was first seen
    # anywhere in the process - so an attribute set comes back in
    # whatever order its names happened to be interned.
    # `lexicographicOrder` is the accessor that hides it, and the
    # Bridge caches the result because the walk reads every index
    # against one object.
    #
    # A list[Value] or dict[str, Value] accessor is deliberately
    # absent. It needs a collection of PROXIES, which is the recursive
    # value message (tasks/030), not another loop here.

    def size(self) -> I64:
        """Elements in a list, or attributes in an attribute set."""

    def at(self, index: I64) -> "Value":
        """One element of a list.

        It may still be a thunk: forcing a list forces the list, not
        what is in it."""

    def name_at(self, index: I64) -> Str:
        """One attribute name, in alphabetical order."""

    def value_at(self, index: I64) -> "Value":
        """One attribute value, in alphabetical order of name."""

    def has(self, name: Str) -> Bint:
        """Whether this attribute set carries that name."""

    # `get` in Python, `get_attr` in C++. The Bridge already has a
    # `get()` - it hands back the rooted nix::Value - and that one is
    # plumbing rather than surface.
    @cxx_name("get_attr")
    def get(self, name: Str) -> "Value":
        """One attribute by name. Raises when it is missing."""


@header("cythonix_bindings/_cpp/eval.hpp")
@binding(
    cxx="cythonix::Evaluator",
    # Not thread-safe, one per thread. libexpr says so and this is
    # where that sentence becomes a policy.
    threading="affine",
    # Parsing and evaluating are slow and pure C++ after the string
    # crosses. The accessors are not, and say so.
    blocking=True,
)
class EvalState:
    """One evaluator, and the thread it belongs to."""

    def __init__(self, store_uri: Str) -> None:
        """Open a state against a store URI.

        REQUIRED, with no default. A state is bound to a store and a
        thread, and neither is a thing to guess at.

        `nix::EvalState` takes a `ref<Store>` and two settings objects
        that must outlive it, so `cythonix::Evaluator` owns all four
        and this parameter is the one a caller can answer."""

    def get_store_uri(self) -> Str:
        """The URI this state was opened with."""

    def parse_expr(self, expr: Str) -> "Value":
        """Parse without evaluating: the result is an unforced
        thunk."""

    def eval_expr(self, expr: Str) -> "Value":
        """Parse and evaluate: slow, fully forced result."""

    def force(self, v: "Value") -> None:
        """Force a value in place. Idempotent.

        Mutates GC-resident memory, and the async layer routes the
        call to this state's OWN thread - which is why a Value is
        affine and travels as a proxy."""

    # Builders. A caller builds a list or an attribute set one element
    # at a time, and that shape is the WIRE's, not libexpr's: a Nix
    # collection is immutable and sized when it is built, so each call
    # here rebuilds it.
    #
    # The alternative is worse. `make_list(items: list[Value])` would
    # need a container of PROXIES to cross, and one lease per element
    # is not something anything grants in bulk - so it would have no
    # RPC surface at all. One element per call is what crosses.

    def make_int(self, value: I64) -> "Value":
        """A forced integer value."""

    def make_string(self, value: Str) -> "Value":
        """A forced string value, with no string context."""

    def make_bool(self, value: Bint) -> "Value":
        """A forced boolean value."""

    def make_list(self) -> "Value":
        """An empty list. Fill it with `list_append`."""

    def list_append(self, target: "Value", item: "Value") -> None:
        """Add one element to a list, in place."""

    def make_attrs(self) -> "Value":
        """An empty attribute set. Fill it with `attrs_set`."""

    def attrs_set(self, target: "Value", name: Str, item: "Value") -> None:
        """Set one attribute, in place.

        Setting a name twice replaces its value, matching an attribute
        set built by assignment."""


# --- free functions ------------------------------------------------


@needs("cythonix_bindings/_cpp/eval.hpp")
@threading("pool")
def gc_stats() -> "dict[str, int]":
    """Live collector counters, bound straight from gc.h.

    These prove the collector is ACTIVE: a no-op integration cannot
    fake them."""
    Cxx("""
nb::dict out;
out["heap_size"] = GC_get_heap_size();
out["total_bytes"] = GC_get_total_bytes();
out["bytes_since_gc"] = GC_get_bytes_since_gc();
out["collections"] = static_cast<std::size_t>(GC_get_gc_no());
out["used_bytes"] = GC_get_heap_size() - GC_get_free_bytes();
// OURS, not the collector's: how many roots this process holds. A
// root that is never dropped keeps its value alive forever, and no
// heap counter can tell that from a heap that simply grew.
out["live_roots"] = cythonix::live_roots().load();
return out;
    """)


@needs("cythonix_bindings/_cpp/eval.hpp")
@threading("pool")
@blocks
@binds("cythonix::gc_collect")
def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice).

    Global process state, mirroring libgc: not a method on EvalState.
    Blocking - dispatch it to a thread from async code.

    It registers the calling thread first. Boehm stops the world by
    signalling every registered thread, and a thread it does not know
    cannot answer - the collection aborts the process with "Collecting
    from unknown thread"."""


@needs("cythonix_bindings/_cpp/eval.hpp")
@binds("cythonix::gc_unregister_thread")
def gc_release_thread() -> None:
    """Take the CURRENT thread off the collector's list.

    Runtime plumbing, not domain surface: it carries no threading
    policy, so the codegen leaves it alone and it has no async or RPC
    form. The absence is the declaration.

    A thread that registered must call this as its last GC action
    before it exits. Boehm stops the world by signalling every
    registered thread and waiting for each to answer. A thread that
    exits while still registered never answers, and the next
    collection aborts the process. That is what a dedicated affine
    executor does when its wrapper is closed."""


@needs("cythonix_bindings/_cpp/eval.hpp")
@binds("nix::initGC")
@startup
def _gc_init() -> None:
    """Start the collector, once, before any value can exist.

    Bound straight from libexpr. It also calls
    `GC_allow_register_threads`, so the permission is upstream's and
    only the per-thread registration is ours."""
