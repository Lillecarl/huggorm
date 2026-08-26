# cython: language_level=3
# cython: annotation_typing=False
# The first binding of a REAL Nix type (tasks/015).
#
# nix::StorePath is the smallest thing that proves the whole chain:
# pkg-config linkage against libnixstore, a namespaced C++ class, a
# constructor that validates and throws, and accessors returning views
# into the object's own storage.
#
# It stands beside the mock's StorePath rather than replacing it. The
# mock still backs everything else, and a spike that broke the working
# surface would prove nothing.

import functools

from cythonix_bindings import _value

from cython.operator cimport dereference as deref
from libcpp.string cimport string
from libcpp.string_view cimport string_view

from cythonix_bindings.c_path cimport CStorePath


cdef inline str _view(string_view v):
    """A view into C++ storage, copied into a Python str.

    Every nix::StorePath accessor hands back a view of the object's own
    baseName. Holding one past the object is a dangling pointer rather
    than an exception, so nothing that leaves this file is ever a view."""
    return v.data()[:v.size()].decode('utf-8')


@functools.total_ordering
cdef class StorePath:
    """A real nix::StorePath.

    Constructible, unlike the mock's StorePath, because the real class
    has a public constructor that takes a base name and validates it.
    That is the honest surface: `StorePath("<hash>-<name>")` either
    gives a store path or raises."""

    # pool: nothing here blocks or touches shared state.
    _threading = "pool"
    # The C++ declaration this class binds; see c_nix_store.pxd.
    _binds = "CStorePath"
    # Every method is a substring of a string already in memory, so
    # there is nothing to release the GIL for and no thread to hop to.
    _blocking = False
    # It serializes: the base name IS the value.
    _wire = "value"
    _wire_fields = (("base_name", "str"),)

    def __init__(self, str base_name):
        # __init__, not __cinit__: __cinit__ runs on every __new__,
        # including the argument-less one a copy needs, so it cannot
        # also be where construction happens.
        cdef string c_name = base_name.encode('utf-8')
        # Raises when the name is not a store path. The message comes
        # from libstore, which is the whole point of binding it.
        self._ptr = new CStorePath(c_name)

    def __dealloc__(self):
        del self._ptr

    cdef inline CStorePath* _get(self) except NULL:
        """The underlying path, or a clear error.

        Reachable only through __new__ without __init__, which is what
        a copy does before it assigns. Without this the next accessor
        dereferences NULL and takes the process with it."""
        if self._ptr is NULL:
            raise ValueError("this StorePath was never constructed")
        return self._ptr

    def __copy__(self):
        cdef StorePath c = StorePath.__new__(StorePath)
        c._ptr = new CStorePath(self._get()[0])
        return c

    def __deepcopy__(self, memo):
        # A store path is immutable: deep copy == copy.
        return self.__copy__()

    # A VALUE compares, hashes and prints as the thing it is. Without
    # these, two paths naming the same store object were never equal,
    # a set of them deduplicated nothing, and repr() showed an address
    # instead of the one string the object IS - so every caller
    # compared .to_string() by hand, this repo's own tests included
    # (tasks/046).
    #
    # The comparison is C++'s, not a Python one on the base name.
    # Upstream defaults both operators, so today they agree; declaring
    # the operator means the binding follows if that ever stops being
    # true.
    def __eq__(self, other):
        if not isinstance(other, StorePath):
            return NotImplemented
        return deref(self._get()) == deref((<StorePath>other)._get())

    def __lt__(self, other):
        # Ordering, so sorted() works and total_ordering can fill in
        # the rest. Nix keeps store paths in sets, which are sorted,
        # so a caller who sorts is matching the store's own order.
        if not isinstance(other, StorePath):
            return NotImplemented
        return deref(self._get()) < deref((<StorePath>other)._get())

    def __hash__(self):
        # Defining __eq__ would otherwise make this unhashable, and a
        # store path is exactly the kind of thing that belongs in a
        # set. The declared part IS the base name, so this agrees with
        # __eq__ by construction.
        return _value.hash_(self)

    def __repr__(self):
        return _value.repr_(self)

    def __str__(self):
        return self.to_string()

    def to_string(self) -> str:
        """The full base name, '<hash>-<name>'."""
        return _view(self._get().to_string())

    def name(self) -> str:
        """The part after the hash."""
        return _view(self._get().name())

    def hash_part(self) -> str:
        """The 32-character base-32 hash."""
        return _view(self._get().hash_part())

    def is_derivation(self) -> bint:
        """Whether the name ends in '.drv'."""
        return self._get().is_derivation()

    @classmethod
    def _from_parts(cls, str base_name):
        """Wire-deserialization helper (private, never surfaced by the
        codegen). The constructor already validates, so this is it."""
        return StorePath(base_name)

    def _parts(self):
        """Wire-serialization helper (private): one value per
        _wire_fields entry, in order."""
        return (self.to_string(),)
