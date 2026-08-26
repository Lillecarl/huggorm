# cython: language_level=3
from libcpp.string cimport string
from libcpp.string_view cimport string_view

cdef extern from "thing.hpp" namespace "probe" nogil:
    cdef cppclass CThing "probe::Thing":
        CThing(const CThing & other)
        CThing(string n) except +
        string_view name() except +
        bint operator==(const CThing & other)
