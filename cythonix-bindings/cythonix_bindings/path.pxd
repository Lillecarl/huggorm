# cython: language_level=3
# So another module can reach a StorePath's pointer. Cython needs a
# pxd for a cdef class the moment a second module touches its fields,
# and Store does: it validates and prints them.

from cythonix_bindings.c_path cimport CStorePath


cdef class StorePath:
    cdef CStorePath* _ptr

    cdef inline CStorePath* _get(self) except NULL
