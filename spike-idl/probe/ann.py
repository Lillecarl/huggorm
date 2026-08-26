import cython
from cython.cimports.c_thing import CThing
from cython.cimports.libcpp.memory import make_shared, shared_ptr
from cython.cimports.libcpp.string import string


@cython.cclass
class Thing:
    _ptr: shared_ptr[CThing]

    def __init__(self, name: str):
        c_name: string = name.encode('utf-8')
        self._ptr = make_shared[CThing](c_name)

