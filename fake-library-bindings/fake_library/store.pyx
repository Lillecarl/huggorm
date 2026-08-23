# cython: language_level=3
# cython: annotation_typing=False
# This is the "pyx" - the implementation that bridges Python <-> C++ for
# the Nix-store mock.
#
# Layout mirrors the real domain:
# - PyStore trampoline (verbatim C++ via extern-from-*): forwards the pure
#   virtual get_uri() to Python so subclasses of Store are visible to C++
#   dispatch (describe_store).
# - ONE cdef class Store holds Store* and declares every wrapped method
#   exactly once. LocalStore/RemoteStore are constructor-only subclasses
#   picking which C++ object to allocate.
# - Value types (StorePath, Derivation, DerivedPath) are produced by store
#   methods, never constructed directly.
#
# Threading policies (`_threading`, consumed by the async codegen):
# - LocalStore "pool": real stores are shared objects guarded by locks;
#   concurrent use from several threads is legitimate.
# - RemoteStore "affine": a connection-bound store owns its IO thread;
#   operations serialize there. This is the experiment's affine exemplar.
# - StorePath/DerivedPath "pool": immutable values.
# - Derivation "affine": mutable builder state; ops stay on the producer's
#   thread (the returned-value attachment rule makes this enforceable).
# `_async = False` excludes the abstract base from generation.

from libcpp.string cimport string
from cython.operator cimport dereference as deref

from fake_library.c_store cimport (
    CStore,
    CStorePath,
    CDerivation,
    CDerivedPath,
    CLocalStore,
    CRemoteStore,
    describe_store,
)

# --- Trampoline: C++ class that forwards virtuals to Python ---
cdef extern from *:
    """
    #include "fake_library/store.hpp"
    #include <Python.h>
    #include <string>

    // Lives only in the binding. Lets Python classes that inherit from
    // Store override get_uri() and have C++ code see the override.
    struct PyStore : public fake_library::Store {
        PyObject* py_self;
        explicit PyStore(PyObject* self) : py_self(self) {
            Py_INCREF(py_self);
        }
        ~PyStore() {
            PyGILState_STATE g = PyGILState_Ensure();
            Py_DECREF(py_self);
            PyGILState_Release(g);
        }
        std::string get_uri() const override {
            PyGILState_STATE g = PyGILState_Ensure();
            PyObject* res = PyObject_CallMethod(py_self, "get_uri", NULL);
            std::string out;
            if (res) {
                if (PyUnicode_Check(res)) {
                    PyObject* b = PyUnicode_AsEncodedString(res, "utf-8", "strict");
                    if (b) { out = PyBytes_AsString(b); Py_DECREF(b); }
                }
                Py_DECREF(res);
            } else {
                PyErr_Clear();
                out = "";
            }
            PyGILState_Release(g);
            return out;
        }
    };
    """
    cdef cppclass PyStore(CStore):
        PyStore(object self)


# --- One wrapper class; every method declared once ---
# Allocation rules (same reasoning as any Cython binding of abstract C++):
# - A base __cinit__ ALWAYS runs before the leaf's and cannot be skipped,
#   so the base must not allocate here. Leaves allocate their own C++
#   object in their own __cinit__; the base stays leaf-agnostic.
# - Python subclasses of Store have no allocating __cinit__, so _ptr is
#   still NULL by __init__ time; the base __init__ then installs the
#   trampoline. Dealloc and init paths are NULL-guarded.
cdef class Store:
    cdef CStore* _ptr

    _threading = "pool"
    _async = False  # abstract: excluded from wrapper generation

    def __init__(self):
        if self._ptr == NULL:
            self._ptr = new PyStore(<object>self)

    def __dealloc__(self):
        if self._ptr != NULL:
            del self._ptr

    # GIL policy per method - only the binding knows whether a C++
    # implementation may re-enter Python or how long it runs:
    # - get_uri/is_valid_path: KEEP the GIL. Fast, and get_uri re-enters
    #   Python for trampoline-backed instances.
    # - add_text_to_store/build_derivation/query_derivation: RELEASE.
    #   Potentially slow, pure C++ after arguments are converted.
    def get_uri(self) -> str:
        return self._ptr.get_uri().decode('utf-8')

    def is_valid_path(self, StorePath path) -> bool:
        return self._ptr.is_valid_path(deref(path._ptr))

    def add_text_to_store(self, str name, str contents) -> StorePath:
        cdef string c_name = name.encode('utf-8')
        cdef string c_contents = contents.encode('utf-8')
        cdef StorePath result = StorePath.__new__(StorePath)
        with nogil:
            result._ptr = new CStorePath(self._ptr.add_text_to_store(c_name, c_contents))
        return result

    def build_derivation(self, DerivedPath request) -> StorePath:
        cdef CDerivedPath* c_req = request._ptr
        cdef StorePath result = StorePath.__new__(StorePath)
        with nogil:
            result._ptr = new CStorePath(self._ptr.build_derivation(deref(c_req)))
        return result

    def query_derivation(self, StorePath drv_path) -> Derivation:
        """Parse a .drv previously added to this store. The returned
        derivation carries mutable state: callers must keep invoking it
        on this store's thread (the async layer enforces that)."""
        cdef Derivation drv = Derivation.__new__(Derivation)
        with nogil:
            drv._ptr = new CDerivation(self._ptr.query_derivation(deref(drv_path._ptr)))
        return drv


# --- Concrete subclasses ---
# Each leaf allocates its own C++ object in its own __cinit__. The base
# contributes nothing to the chain.

cdef class LocalStore(Store):
    _threading = "pool"

    def __cinit__(self):
        self._ptr = new CLocalStore()


cdef class RemoteStore(Store):
    _threading = "affine"

    def __cinit__(self):
        self._ptr = new CRemoteStore()


# --- Value types ---
# Never constructed by Python directly: instances come from store methods
# via __new__ (which skips __init__). The _threading marker tells the
# codegen how returned instances may be accessed.

cdef class StorePath:
    _threading = "pool"
    # Immutable value: safe to serialize across a wire, so it crosses
    # wrapper boundaries as a copy.
    _wire = "value"

    cdef CStorePath* _ptr

    def __init__(self):
        raise TypeError("StorePath instances are produced by stores, not constructed")

    def __dealloc__(self):
        del self._ptr

    def __copy__(self):
        cdef StorePath c = StorePath.__new__(StorePath)
        c._ptr = new CStorePath(deref(self._ptr))
        return c

    def __deepcopy__(self, memo):
        # Immutable: deep copy == copy.
        return self.__copy__()

    def to_string(self) -> str:
        return self._ptr.to_string().decode('utf-8')

    def hash_part(self) -> str:
        return self._ptr.hash().decode('utf-8')

    def name_part(self) -> str:
        return self._ptr.name().decode('utf-8')


cdef class Derivation:
    _threading = "affine"
    # The instructive wire case: looks like a value, but set_env and the
    # access counter mutate it - so despite being a plain data holder it
    # must travel as a proxy. Mutability forces proxy, always.
    _wire = "proxy"

    cdef CDerivation* _ptr

    def __init__(self):
        raise TypeError("Derivation instances come from query_derivation")

    def __dealloc__(self):
        del self._ptr

    def set_env(self, str key, str value):
        cdef string c_key = key.encode('utf-8')
        cdef string c_val = value.encode('utf-8')
        self._ptr.set_env(c_key, c_val)

    def describe(self) -> str:
        # Mutates an internal counter: this is why the type is affine.
        return self._ptr.describe().decode('utf-8')

    def queries(self) -> int:
        return self._ptr.queries()


cdef class DerivedPath:
    _threading = "pool"
    # Immutable build request: wire-value.
    _wire = "value"

    cdef CDerivedPath* _ptr

    def __cinit__(self, StorePath path=None, str output=None):
        # Optional args exist only so __copy__ can allocate via __new__
        # (tp_new always runs __cinit__); real construction validates
        # in __init__ below.
        if path is None:
            self._ptr = NULL
            return
        if output is None:
            self._ptr = new CDerivedPath(deref((<StorePath>path)._ptr))
        else:
            self._ptr = new CDerivedPath(deref((<StorePath>path)._ptr), output.encode('utf-8'))

    def __init__(self, StorePath path=None, str output=None):
        if path is None:
            raise TypeError("DerivedPath requires a StorePath")

    def __dealloc__(self):
        if self._ptr != NULL:
            del self._ptr

    def __copy__(self):
        cdef DerivedPath c = DerivedPath.__new__(DerivedPath)
        c._ptr = new CDerivedPath(deref(self._ptr))
        return c

    def __deepcopy__(self, memo):
        # Immutable: deep copy == copy.
        return self.__copy__()

    def describe(self) -> str:
        return self._ptr.describe().decode('utf-8')


def describe(obj) -> str:
    """C++ free function describe_store(const Store&) - goes through C++
    virtual dispatch. Sees get_uri overrides on trampoline subclasses
    (Python subclasses of Store), not on plain-Python overrides of
    LocalStore/RemoteStore."""
    if not isinstance(obj, Store):
        raise TypeError(f"describe() expects a Store, got {type(obj)}")
    cdef string res = describe_store(deref((<Store>obj)._ptr))
    return res.decode('utf-8')
