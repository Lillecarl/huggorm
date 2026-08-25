# cython: language_level=3
# Declaration of the C++ evaluation API (include/fake_library/eval.hpp).
# Parsed alongside c_store.pxd by the codegen; EvalState and Value are
# the affine exemplars of the service and returned-value roles.

from libcpp.string cimport string
from libc.stdint cimport int64_t
from libc.stddef cimport size_t

# eval.hpp must come first: it pulls in gc-env.hpp, which defines
# GC_THREADS before gc.h is ever included. Reversed, gc.h processes
# without thread support and its include guard hides the registration
# API from every later consumer.
cdef extern from "fake_library/eval.hpp" nogil:
    cdef cppclass CValue "fake_library::Value":
        CValue() except +
        CValue(const CValue & other)
        string type_name() const
        # Note: the real accessors are const; Cython's parser does not
        # accept const combined with except +, and these are only ever
        # called through non-const pointers.
        int64_t integer() except +
        string string_value() except +
        bint boolean() except +
        # Collections, by index. No container appears in any signature:
        # a template type renders as itself here and maps to nothing, so
        # the declared surface is scalars and Value pointers.
        size_t size() except +
        CValue * at(size_t index) except +
        # Attributes are stored in name order, so an index walk is the
        # alphabetical listing Nix guarantees.
        string name_at(size_t index) except +
        CValue * value_at(size_t index) except +
        bint has(string name) except +
        CValue * get(string name) except +

    cdef cppclass CEvalState "fake_library::EvalState":
        CEvalState(string store_uri)
        string get_store_uri() const
        # Values are arena-resident: the state returns borrowed pointers
        # and nothing is ever deleted (the collector owns them when the
        # Boehm build is active).
        CValue * parse_expr(string expr) except + nogil
        CValue * eval_expr(string expr) except + nogil
        void force(CValue * v) except + nogil
        # Builders. Nix hands back attribute sets far more often than
        # scalars, and the wire has to carry one; these produce the
        # shapes without a parser for them. Collections fill one element
        # at a time, which is also what keeps every intermediate
        # reachable: the collector sees an element through the value it
        # was added to.
        CValue * make_int(int64_t v) except + nogil
        CValue * make_string(string v) except + nogil
        CValue * make_bool(bint v) except + nogil
        CValue * make_list() except + nogil
        void list_append(CValue * target, CValue * item) except + nogil
        CValue * make_attrs() except + nogil
        void attrs_set(CValue * target, string name, CValue * item) except + nogil

    void gc_init "fake_library::gcenv::init" ()
    void gc_register_current_thread "fake_library::gcenv::register_current_thread" ()
    void gc_unregister_current_thread "fake_library::gcenv::unregister_current_thread" ()
    void gc_collect "fake_library::gcenv::collect" ()

# Bound directly - no wrapper layer. Safe only AFTER gc.h has been
# included with GC_THREADS set (see note above).
cdef extern from "gc/gc.h" nogil:
    size_t GC_get_heap_size()
    size_t GC_get_total_bytes()
    size_t GC_get_free_bytes()
    size_t GC_get_bytes_since_gc()
    unsigned long GC_get_gc_no()
    void * GC_base(void * p)
    # Uncollectable but SCANNED: the wrapper bridge cells. They keep
    # values visible exactly as long as their Python wrapper exists.
    void * GC_malloc_uncollectable(size_t)
    void GC_free(void *)
