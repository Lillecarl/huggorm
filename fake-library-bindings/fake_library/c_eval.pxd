# cython: language_level=3
# Declaration of the C++ evaluation API (include/fake_library/eval.hpp).
# Parsed alongside c_store.pxd by the codegen; EvalState and Value are
# the affine exemplars of the service and returned-value roles.

from libcpp.string cimport string
from libc.stdint cimport int64_t

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

    cdef cppclass CEvalState "fake_library::EvalState":
        CEvalState(string store_uri)
        string get_store_uri() const
        CValue parse_expr(string expr) except + nogil
        CValue eval_expr(string expr) except + nogil
        void force(CValue & v) except + nogil
