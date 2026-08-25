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
from cython.operator cimport dereference as deref

from fake_library.c_eval cimport (
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
    gc_collect,
)

# Register this thread with the collector (and initialize it) before any
# value can exist.
gc_init()


cdef class Value:
    _threading = "affine"
    # Wire-proxy despite being "just data": thunks must force on their
    # home thread and forcing mutates in place. A future refinement may
    # serialize forced scalars; until then, proxy.
    _wire = "proxy"

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


cdef class EvalState:
    cdef CEvalState* _ptr

    _threading = "affine"

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
        cdef Value v = Value.__new__(Value)
        # This may run on a runner thread created by Python: register
        # it with the collector before touching GC memory.
        gc_register_current_thread()
        v._cell = <CValue **>GC_malloc_uncollectable(sizeof(CValue *))
        with nogil:
            v._cell[0] = self._ptr.parse_expr(c_expr)
        return v

    def eval_expr(self, str expr) -> Value:
        """Parse and evaluate: slow, fully forced result."""
        cdef string c_expr = expr.encode('utf-8')
        cdef Value v = Value.__new__(Value)
        gc_register_current_thread()
        v._cell = <CValue **>GC_malloc_uncollectable(sizeof(CValue *))
        with nogil:
            v._cell[0] = self._ptr.eval_expr(c_expr)
        return v

    def force(self, Value v) -> None:
        """Force a value in place. Idempotent."""
        # Forcing mutates GC-resident memory, and the async layer may
        # route this call through any worker of the state's runner.
        gc_register_current_thread()
        self._ptr.force(v._cell[0])


def gc_stats() -> dict:
    """Live collector counters, bound straight from gc.h. These prove
    the collector is ACTIVE: a no-op integration cannot fake them."""
    return {
        "heap_size": GC_get_heap_size(),
        "total_bytes": GC_get_total_bytes(),
        "bytes_since_gc": GC_get_bytes_since_gc(),
        "collections": GC_get_gc_no(),
        "used_bytes": GC_get_heap_size() - GC_get_free_bytes(),
    }


def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice). No-op without
    Boehm GC. Global process state, mirroring libgc: not a method on
    EvalState. Blocking - dispatch it to a thread from async code
    (asyncio.to_thread)."""
    gc_register_current_thread()
    gc_collect()
