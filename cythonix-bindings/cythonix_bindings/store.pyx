# cython: language_level=3
# cython: annotation_typing=False
# The real nix::Store (tasks/015).
#
# Abstract in C++, and its implementation is chosen by a URI, so the
# binding is constructed through openStore rather than a constructor.
# "dummy://" is an in-memory store and needs nothing on disk, which is
# what makes this testable in a build sandbox.

from cython.operator cimport dereference as deref
from libcpp.memory cimport shared_ptr
from libcpp.string cimport string

from cythonix_bindings.c_path cimport CStorePath
from cythonix_bindings.c_store cimport (
    CStore,
    init_libstore,
    open_store,
    parse_store_path,
    store_uri,
)
from cythonix_bindings.path cimport StorePath

# Before anything reaches libstore. Importing this module is the first
# moment that can happen, and libstore aborts rather than raises if it
# has not - so there is no later point that would still be safe.
init_libstore()


cdef class Store:
    """A real nix::Store, opened from a URI.

    `Store("dummy://")` is in-memory. `Store("auto")` is whatever the
    ambient configuration says, which usually means the daemon."""

    # A store talks to a daemon or a database, so its calls block and
    # it needs a thread to hop off the loop onto. Nix stores carry
    # their own locking, so any pool thread will do.
    _threading = "pool"
    _binds = "CStore"
    # Identity matters and it is not serializable: a store is a
    # connection, not a value.
    _wire = "proxy"
    # There is no constructor to read a signature from - nix::Store is
    # abstract. openStore is the factory, and its parameters are the
    # ones a caller passes.
    _ctor_from = "open_store"

    # shared_ptr, not a raw pointer: openStore hands back a ref<Store>,
    # which is a shared_ptr that cannot be null, and the store has to
    # stay alive as long as this wrapper does.
    cdef shared_ptr[CStore] _store

    def __init__(self, str uri):
        cdef string c_uri = uri.encode('utf-8')
        with nogil:
            self._store = open_store(c_uri)

    cdef inline CStore* _get(self) except NULL:
        if not self._store:
            raise ValueError("this Store was never opened")
        return self._store.get()

    def get_uri(self) -> str:
        """How this store describes itself.

        For logging only, upstream is explicit about that: it does not
        round-trip as a store reference and it is not a cache key."""
        return store_uri(deref(self._get())).decode('utf-8')

    def is_valid_path(self, StorePath path) -> bint:
        """Whether the store has that path."""
        cdef CStore* store = self._get()
        cdef CStorePath* p = path._get()
        cdef bint out
        with nogil:
            out = store.is_valid_path(deref(p))
        return out

    def print_store_path(self, StorePath path) -> str:
        """The path as an absolute filesystem path in this store."""
        cdef CStore* store = self._get()
        cdef CStorePath* p = path._get()
        cdef string out
        with nogil:
            out = store.print_store_path(deref(p))
        return out.decode('utf-8')

    def parse_store_path(self, path: str) -> StorePath:
        """An absolute path in this store, as a StorePath.

        Raises when it is not in this store's directory - which is a
        different question from whether the name is well formed, and
        the reason this lives on the store rather than on StorePath."""
        cdef string c_path = path.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = parse_store_path(deref(store), c_path)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp
