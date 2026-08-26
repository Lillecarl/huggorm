# The three verifications, in one pure-mode file.
import cython
from cython.cimports.c_thing import CThing, make_thing
from cython.cimports.libcpp.memory import shared_ptr
from cython.cimports.libcpp.string import string
from cython.operator import dereference as deref


@cython.cclass
class Thing:
    """1. Does `except +translate_probe_error` fire from pure mode?

    The declaration carries it, and the declaration is a pxd either
    way - so pure mode never has to spell it."""

    _ptr: shared_ptr[CThing]

    def __init__(self, name: str):
        c_name: string = name.encode('utf-8')
        self._ptr = make_thing(c_name)

    def name(self) -> str:
        v = deref(self._ptr).name()
        return v.data()[:v.size()].decode('utf-8')

    def slow_len(self) -> int:
        """2. nogil inside a `def`, which is where the real bindings
        put it - not only inside a cfunc."""
        n: cython.size_t = 0
        p: shared_ptr[CThing] = self._ptr
        with cython.nogil:
            n = deref(p).name().size()
        return n


@cython.cclass
class Located:
    """3. The produced-value shape: no C++ at all, Python slots that
    something else fills, and an __init__ that refuses."""

    _path: object
    _sub_path: object

    def __init__(self):
        raise TypeError("Located comes from Thing.locate, not a constructor")

    def path(self) -> object:
        return self._path

    def sub_path(self) -> object:
        return self._sub_path

    @classmethod
    def _from_parts(cls, path, sub_path):
        loc: Located = Located.__new__(Located)
        loc._path = path
        loc._sub_path = sub_path
        return loc

    def _parts(self):
        return (self._path, self._sub_path)
