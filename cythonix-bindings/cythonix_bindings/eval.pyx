# cython: language_level=3
# cython: annotation_typing=False
# The evaluation binding: EvalState is the affine SERVICE exemplar.
#
# The real EvalState is documented as not thread-safe, one per thread;
# here that fact becomes `_threading = "affine"`. Values live on the
# state's thread: they are produced by state methods and attach to its
# runner in the async layer. force() mutates a value in place - the
# affine-value-as-parameter case.

from libcpp.string cimport string
from libc.stdint cimport uintptr_t
from cython.operator cimport dereference as deref

from cythonix_bindings.c_eval cimport (
    CEvalState,
    CValue,
    GC_base,
    GC_free,
    GC_get_bytes_since_gc,
    GC_get_free_bytes,
    GC_get_gc_no,
    GC_get_heap_size,
    GC_get_total_bytes,
    GC_malloc_uncollectable,
    gc_init,
    gc_register_current_thread,
    gc_unregister_current_thread,
    gc_collect,
)

# The declaration goes ON the function. A cdef class cannot take a
# decorator at all - Cython allows exactly two - so a class declares
# itself in its body and a free function declares itself here.
from cythonix_bindings._declare import threading

# Register this thread with the collector (and initialize it) before any
# value can exist.
gc_init()


cdef Value _bridge(CValue * ptr):
    """A Python wrapper over one GC-resident value.

    The caller must have registered this thread first: the bridge cell
    comes from the collector, and allocating from an unregistered
    thread races with a collection. `ptr` itself survives that
    allocation because a registered thread's stack is scanned."""
    cdef Value v = Value.__new__(Value)
    v._cell = <CValue **>GC_malloc_uncollectable(sizeof(CValue *))
    v._cell[0] = ptr
    return v


cdef class Value:
    _threading = "affine"
    # The C++ declaration this class binds; see store.pyx.
    _binds = "CValue"
    # Wire-proxy despite being "just data": thunks must force on their
    # home thread and forcing mutates in place. A future refinement may
    # serialize forced scalars; until then, proxy.
    _wire = "proxy"

    # How a value TREE is walked, read by the RPC layer so that no
    # layer above this file knows what a Value is or which of its
    # methods do what (tasks/030). `kind` names the accessor that says
    # what this node is; its answer selects one of the branches below.
    # A kind named nowhere here - a thunk - crosses as a proxy, which
    # is exactly the laziness the wire cannot serialize.
    _tree = {
        "kind": "type_name",
        # What makes two nodes THE SAME node. A fresh Python wrapper is
        # built for every access, so Python identity says nothing: two
        # wrappers over one value differ, and a wrapper that dies hands
        # its id() to the next one. The underlying object is the
        # identity, and only this file can say where it lives.
        "identity": "_identity",
        # kind reported by `kind` -> [wire type, accessor]. The wire
        # type is what picks the arm, so the layer above reads a
        # declared type name rather than a label this file invented.
        "scalars": {"int": ["int", "integer"],
                    "string": ["str", "string_value"],
                    "bool": ["bool", "boolean"]},
        "list": {"size": "size", "item": "at"},
        "attrs": {"size": "size", "name": "name_at", "value": "value_at"},
    }

    # Single bridge field: an uncollectable GC cell holding the CValue
    # pointer. The collector scans the cell, so the value stays alive
    # exactly as long as this wrapper does - and becomes reclaimable,
    # even while its EvalState lives on, once the wrapper dies and the
    # cell is freed. Every access dereferences the cell.
    cdef CValue** _cell

    def __init__(self):
        raise TypeError("Value instances come from an EvalState")

    def __dealloc__(self):
        # Freeing the bridge drops the last visible reference; the
        # collector owns the value itself from birth to death.
        #
        # This runs on WHICHEVER thread drops the last Python reference,
        # which is rarely the producing runner: the asyncio loop thread,
        # a pool worker, or the server's reaper. GC_free takes the
        # collector's lock and may have to cooperate with a collection,
        # so the thread must be registered first. Reading a value needs
        # no registration - the uncollectable cell keeps it reachable
        # from anywhere - but freeing does.
        if self._cell != NULL:
            gc_register_current_thread()
            GC_free(self._cell)

    def _identity(self) -> int:
        """The underlying value's address, as a number.

        Private: it is not surface, so the codegen leaves it out of
        every generated form. The tree walk uses it to visit a shared
        value once - values are immutable and shared freely, so without
        it a diamond is copied and a cycle never ends."""
        return <uintptr_t>self._cell[0]

    def is_gc_managed(self) -> bint:
        """True when this value lives inside a GC-allocated block.
        Bound straight from gc.h: a no-op integration cannot fake it."""
        return GC_base(self._cell[0]) != NULL

    def type_name(self) -> str:
        return self._cell[0].type_name().decode('utf-8')

    def integer(self) -> int:
        return self._cell[0].integer()

    def string_value(self) -> str:
        return self._cell[0].string_value().decode('utf-8')

    def boolean(self) -> bint:
        return self._cell[0].boolean()

    # Collections. Reading is by index, which is also how the
    # alphabetical order of an attribute set reaches Python: the C++
    # side keeps attributes in name order, like nix::Bindings.
    #
    # A list[Value] or dict[str, Value] accessor is deliberately absent.
    # It needs a collection of PROXIES, which is the recursive value
    # message (tasks/030), not another loop here.

    def size(self) -> int:
        """Elements in a list, or attributes in an attribute set."""
        return self._cell[0].size()

    def at(self, index: int) -> Value:
        """One element of a list. It may still be a thunk: forcing a
        list forces the list, not what is in it."""
        gc_register_current_thread()
        return _bridge(self._cell[0].at(index))

    def name_at(self, index: int) -> str:
        """One attribute name, in alphabetical order."""
        return self._cell[0].name_at(index).decode('utf-8')

    def value_at(self, index: int) -> Value:
        """One attribute value, in alphabetical order of name."""
        gc_register_current_thread()
        return _bridge(self._cell[0].value_at(index))

    def has(self, str name) -> bint:
        """Whether this attribute set carries that name."""
        cdef string c_name = name.encode('utf-8')
        return self._cell[0].has(c_name)

    def get(self, str name) -> Value:
        """One attribute by name. Raises when it is missing."""
        cdef string c_name = name.encode('utf-8')
        gc_register_current_thread()
        return _bridge(self._cell[0].get(c_name))


cdef class EvalState:
    cdef CEvalState* _ptr

    _threading = "affine"
    _binds = "CEvalState"

    def __cinit__(self, str store_uri="local"):
        cdef bytes b_uri = store_uri.encode('utf-8')
        self._ptr = new CEvalState(b_uri)

    def __dealloc__(self):
        if self._ptr != NULL:
            del self._ptr

    def get_store_uri(self) -> str:
        return self._ptr.get_store_uri().decode('utf-8')

    # GIL policy: parsing and evaluating are slow, pure C++ after the
    # string conversion. Release.

    def parse_expr(self, str expr) -> Value:
        """Parse without evaluating: the result is an unforced thunk."""
        cdef string c_expr = expr.encode('utf-8')
        cdef CValue * out
        # This may run on a runner thread created by Python: register
        # it with the collector before touching GC memory.
        gc_register_current_thread()
        with nogil:
            out = self._ptr.parse_expr(c_expr)
        return _bridge(out)

    def eval_expr(self, str expr) -> Value:
        """Parse and evaluate: slow, fully forced result."""
        cdef string c_expr = expr.encode('utf-8')
        cdef CValue * out
        gc_register_current_thread()
        with nogil:
            out = self._ptr.eval_expr(c_expr)
        return _bridge(out)

    def force(self, Value v) -> None:
        """Force a value in place. Idempotent."""
        # Forcing mutates GC-resident memory, and the async layer may
        # route this call through any worker of the state's runner.
        gc_register_current_thread()
        self._ptr.force(v._cell[0])

    # Builders. The expression language is a toy and stays one: an
    # attribute set is BUILT here rather than parsed, because
    # reimplementing Nix's syntax would buy nothing the wire and
    # lifetime paths do not already get from a builder.

    def make_int(self, value: int) -> Value:
        """A forced integer value."""
        gc_register_current_thread()
        return _bridge(self._ptr.make_int(value))

    def make_string(self, str value) -> Value:
        """A forced string value."""
        cdef string c_value = value.encode('utf-8')
        gc_register_current_thread()
        return _bridge(self._ptr.make_string(c_value))

    def make_bool(self, value: bint) -> Value:
        """A forced boolean value."""
        gc_register_current_thread()
        return _bridge(self._ptr.make_bool(value))

    def make_list(self) -> Value:
        """An empty list. Fill it with list_append."""
        gc_register_current_thread()
        return _bridge(self._ptr.make_list())

    def list_append(self, Value target, Value item) -> None:
        """Add one element to a list, in place."""
        gc_register_current_thread()
        self._ptr.list_append(target._cell[0], item._cell[0])

    def make_attrs(self) -> Value:
        """An empty attribute set. Fill it with attrs_set."""
        gc_register_current_thread()
        return _bridge(self._ptr.make_attrs())

    def attrs_set(self, Value target, str name, Value item) -> None:
        """Set one attribute, in place. Setting a name twice replaces
        its value, matching an attribute set built by assignment."""
        cdef string c_name = name.encode('utf-8')
        gc_register_current_thread()
        self._ptr.attrs_set(target._cell[0], c_name, item._cell[0])


@threading("pool")
def gc_stats() -> dict[str, int]:
    """Live collector counters, bound straight from gc.h. These prove
    the collector is ACTIVE: a no-op integration cannot fake them."""
    return {
        "heap_size": GC_get_heap_size(),
        "total_bytes": GC_get_total_bytes(),
        "bytes_since_gc": GC_get_bytes_since_gc(),
        "collections": GC_get_gc_no(),
        "used_bytes": GC_get_heap_size() - GC_get_free_bytes(),
    }


@threading("pool")
def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice). No-op without
    Boehm GC. Global process state, mirroring libgc: not a method on
    EvalState. Blocking - dispatch it to a thread from async code
    (asyncio.to_thread)."""
    gc_register_current_thread()
    gc_collect()


def gc_release_thread() -> None:
    """Take the CURRENT thread off the collector's list.

    Runtime plumbing, not domain surface: it carries no @threading,
    so the codegen leaves it alone and it has no async or RPC form.
    The absence is the declaration, and it is visible here rather than
    as a missing line somewhere else. A thread that registered must
    call this as its last GC action before it exits.

    Boehm stops the world by signalling every registered thread and
    waiting for each to answer. A thread that exits while still
    registered never answers, and the next collection aborts the
    process. That is what a dedicated affine executor does when its
    wrapper is closed."""
    gc_unregister_current_thread()

