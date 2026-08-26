"""
The evaluation binding: EvalState is the affine SERVICE exemplar.

The real EvalState is documented as not thread-safe, one per thread;
here that fact becomes `threading="affine"`. Values live on the
state's thread: they are produced by state methods and attach to its
runner in the async layer. `force` mutates a value in place, which is
the affine-value-as-parameter case.

The library is a mock of libexpr, and the one thing it mocks
faithfully is LIFETIME. A value lives in the collector's heap and
belongs to nobody: it dies when the collector can no longer see a
pointer to it, even while its EvalState lives on. Python's heap is
not scanned, so a wrapper holding a bare pointer roots nothing -
which is what `cythonix::Bridge` is for, and the only C++ in this
binding that a declaration could not have written.
"""

from cythonix_idl.declare import (
    I64,
    Bint,
    Str,
    binding,
    binds,
    blocks,
    cxx_body,
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
    # One state per thread, and its values belong to that thread.
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
# nowhere here - a thunk - crosses as a proxy, which is exactly the
# laziness the wire cannot serialize.
@tree(
    kind="type_name",
    # What makes two nodes THE SAME node. A fresh wrapper is built for
    # every access, so Python identity says nothing: two wrappers over
    # one value differ, and a wrapper that dies hands its id() to the
    # next one. The underlying object is the identity.
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
    """One GC-resident value, rooted for as long as Python holds it.

    Wire-proxy despite being "just data": a thunk must force on its
    home thread and forcing mutates in place. A future refinement may
    serialize forced scalars; until then, proxy.

    Produced, never constructed. A value comes from an EvalState -
    parsed, evaluated or built - and there is nothing a caller could
    correctly make one from."""

    @cxx_body("return static_cast<std::int64_t>(v.identity());")
    def _identity(self) -> I64:
        """The underlying value's address, as a number.

        Private: it is not surface, so the codegen leaves it out of
        every generated form. The tree walk uses it to visit a shared
        value once - values are immutable and shared freely, so
        without it a diamond is copied and a cycle never ends."""

    @cxx_body("return v.is_gc_managed();")
    def is_gc_managed(self) -> Bint:
        """True when this value lives inside a GC-allocated block.

        Bound straight from gc.h: a no-op integration cannot fake
        it."""

    @cxx_body("return v.get()->type_name();")
    def type_name(self) -> Str:
        """"thunk", "int", "string", "bool", "list" or "attrs"."""

    @cxx_body("return v.get()->integer();")
    def integer(self) -> I64:
        """This value as an integer. Raises on a thunk, or on a value
        of another kind."""

    @cxx_body("return v.get()->string_value();")
    def string_value(self) -> Str:
        """This value as a string. Raises as `integer` does."""

    @cxx_body("return v.get()->boolean();")
    def boolean(self) -> Bint:
        """This value as a bool. Raises as `integer` does."""

    # Collections. Reading is by index, which is also how the
    # alphabetical order of an attribute set reaches Python: the C++
    # side keeps attributes in name order, like nix::Bindings.
    #
    # A list[Value] or dict[str, Value] accessor is deliberately
    # absent. It needs a collection of PROXIES, which is the recursive
    # value message (tasks/030), not another loop here.

    @cxx_body("return static_cast<std::int64_t>(v.get()->size());")
    def size(self) -> I64:
        """Elements in a list, or attributes in an attribute set."""

    @cxx_body("return cythonix::Bridge(v.get()->at(index));")
    def at(self, index: I64) -> "Value":
        """One element of a list.

        It may still be a thunk: forcing a list forces the list, not
        what is in it."""

    @cxx_body("return v.get()->name_at(index);")
    def name_at(self, index: I64) -> Str:
        """One attribute name, in alphabetical order."""

    @cxx_body("return cythonix::Bridge(v.get()->value_at(index));")
    def value_at(self, index: I64) -> "Value":
        """One attribute value, in alphabetical order of name."""

    @cxx_body("return v.get()->has(name);")
    def has(self, name: Str) -> Bint:
        """Whether this attribute set carries that name."""

    @cxx_body("return cythonix::Bridge(v.get()->get(name));")
    def get(self, name: Str) -> "Value":
        """One attribute by name. Raises when it is missing."""


@header("cythonix_bindings/_cpp/eval.hpp")
@binding(
    cxx="fake_library::EvalState",
    # Not thread-safe, one per thread. The real libexpr says so and
    # this is where that sentence becomes a policy.
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
        thread, and neither is a thing to guess at - the C++
        constructor takes one and so does this.

        The mock does nothing with the URI beyond remembering it: the
        point of carrying one is that a real EvalState is built over a
        store, and the async layer has to route every call that
        follows onto this state's own thread."""

    @cxx_body("return es.get_store_uri();")
    def get_store_uri(self) -> Str:
        """The URI this state was opened with."""

    @cxx_body("return cythonix::Bridge(es.parse_expr(expr));")
    def parse_expr(self, expr: Str) -> "Value":
        """Parse without evaluating: the result is an unforced
        thunk."""

    @cxx_body("return cythonix::Bridge(es.eval_expr(expr));")
    def eval_expr(self, expr: Str) -> "Value":
        """Parse and evaluate: slow, fully forced result."""

    @cxx_body("es.force(v.get());")
    def force(self, v: "Value") -> None:
        """Force a value in place. Idempotent.

        Mutates GC-resident memory, and the async layer may route the
        call through any worker of this state's runner - which is why
        a Value is affine and travels as a proxy."""

    # Builders. The expression language is a toy and stays one: an
    # attribute set is BUILT here rather than parsed, because
    # reimplementing Nix's syntax would buy nothing the wire and
    # lifetime paths do not already get from a builder.

    @cxx_body("return cythonix::Bridge(es.make_int(value));")
    def make_int(self, value: I64) -> "Value":
        """A forced integer value."""

    @cxx_body("return cythonix::Bridge(es.make_string(value));")
    def make_string(self, value: Str) -> "Value":
        """A forced string value."""

    @cxx_body("return cythonix::Bridge(es.make_bool(value));")
    def make_bool(self, value: Bint) -> "Value":
        """A forced boolean value."""

    @cxx_body("return cythonix::Bridge(es.make_list());")
    def make_list(self) -> "Value":
        """An empty list. Fill it with `list_append`."""

    @cxx_body("es.list_append(target.get(), item.get());")
    def list_append(self, target: "Value", item: "Value") -> None:
        """Add one element to a list, in place."""

    @cxx_body("return cythonix::Bridge(es.make_attrs());")
    def make_attrs(self) -> "Value":
        """An empty attribute set. Fill it with `attrs_set`."""

    @cxx_body("es.attrs_set(target.get(), name, item.get());")
    def attrs_set(self, target: "Value", name: Str, item: "Value") -> None:
        """Set one attribute, in place.

        Setting a name twice replaces its value, matching an attribute
        set built by assignment."""


# --- free functions ------------------------------------------------


@needs("cythonix_bindings/_cpp/eval.hpp")
@threading("pool")
@cxx_body("""nb::dict out;
        out["heap_size"] = cythonix::gc_heap_size();
        out["total_bytes"] = cythonix::gc_total_bytes();
        out["bytes_since_gc"] = cythonix::gc_bytes_since_gc();
        out["collections"] = cythonix::gc_collections();
        out["used_bytes"] = cythonix::gc_heap_size()
            - cythonix::gc_free_bytes();
        return out;""")
def gc_stats() -> "dict[str, int]":
    """Live collector counters, bound straight from gc.h.

    These prove the collector is ACTIVE: a no-op integration cannot
    fake them."""


@needs("cythonix_bindings/_cpp/eval.hpp")
@threading("pool")
@blocks
@cxx_body("""// Boehm stops the world by signalling every registered
        // thread. A thread it does not know cannot answer, and the
        // collection aborts the process with "Collecting from
        // unknown thread".
        fake_library::gcenv::register_current_thread();
        fake_library::gcenv::collect();""")
def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice).

    Global process state, mirroring libgc: not a method on EvalState.
    Blocking - dispatch it to a thread from async code."""


@needs("cythonix_bindings/_cpp/eval.hpp")
@binds("fake_library::gcenv::unregister_current_thread")
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
@binds("fake_library::gcenv::init")
@startup
def _gc_init() -> None:
    """Start the collector, once, before any value can exist."""
