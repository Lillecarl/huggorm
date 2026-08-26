# cython: language_level=3
from libcpp.memory cimport shared_ptr
from libcpp.string cimport string
from libcpp.string_view cimport string_view

cdef extern from "translate.hpp" namespace "probe" nogil:
    cdef void translate_probe_error "probe::translate_probe_error" ()

cdef extern from "thing.hpp" namespace "probe" nogil:
    cdef cppclass CThing "probe::Thing":
        CThing(const CThing & other)
        CThing(string n) except +translate_probe_error
        string_view name() except +translate_probe_error
        bint operator==(const CThing & other)
    # The factory, declared with the translator the constructor's own
    # declaration cannot deliver through make_shared.
    shared_ptr[CThing] make_thing(string n) except +translate_probe_error
