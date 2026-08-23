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
    gc_init,
    gc_register_current_thread,
    gc_collect,
)

# Register this thread with the collector (and initialize it) before any
# value can exist.
gc_init()


cdef class Value:
    _threading = "affine"

    cdef CValue* _ptr

    def __init__(self):
        raise TypeError("Value instances come from an EvalState")

    def __dealloc__(self):
        # Arena-resident: the state owns the value, the collector owns
        # the memory. Deleting here would be wrong under Boehm GC.
        pass

    def type_name(self) -> str:
        return self._ptr.type_name().decode('utf-8')

    def integer(self) -> int:
        return self._ptr.integer()

    def string_value(self) -> str:
        return self._ptr.string_value().decode('utf-8')

    def boolean(self) -> bint:
        return self._ptr.boolean()


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
        with nogil:
            v._ptr = self._ptr.parse_expr(c_expr)
        return v

    def eval_expr(self, str expr) -> Value:
        """Parse and evaluate: slow, fully forced result."""
        cdef string c_expr = expr.encode('utf-8')
        cdef Value v = Value.__new__(Value)
        gc_register_current_thread()
        with nogil:
            v._ptr = self._ptr.eval_expr(c_expr)
        return v

    def force(self, Value v) -> None:
        """Force a value in place. Idempotent."""
        self._ptr.force(v._ptr)


def collect_garbage() -> None:
    """Run a full stop-the-world collection (twice). No-op without
    Boehm GC. Global process state, mirroring libgc: not a method on
    EvalState. Blocking - dispatch it to a thread from async code
    (asyncio.to_thread)."""
    gc_register_current_thread()
    gc_collect()
