# cython: language_level=3
# cython: annotation_typing=False
# This is the "pyx" - the implementation that bridges Python <-> C++ for
# the Nix-store mock.
#
# Layout mirrors the real domain:
# - PyStore trampoline (verbatim C++ via extern-from-*): forwards the pure
#   virtual get_uri() to Python so subclasses of MockStore are visible to C++
#   dispatch (describe_store).
# - ONE cdef class MockStore holds MockStore* and declares every wrapped method
#   exactly once. MockLocalStore/MockRemoteStore are constructor-only subclasses
#   picking which C++ object to allocate.
# - Value types (MockStorePath, MockDerivation, MockDerivedPath) are produced by store
#   methods, never constructed directly.
#
# Threading policies (`_threading`, consumed by the async codegen):
# - MockLocalStore "pool": real stores are shared objects guarded by locks;
#   concurrent use from several threads is legitimate.
# - MockRemoteStore "affine": a connection-bound store owns its IO thread;
#   operations serialize there. This is the experiment's affine exemplar.
# - MockStorePath/MockDerivedPath "pool": immutable values.
# - MockDerivation "affine": mutable builder state; ops stay on the producer's
#   thread (the returned-value attachment rule makes this enforceable).
# `_abstract = True` marks MockStore as a generated BASE: wrapped and
# wire-addressable, but never constructed.
#
# `_blocking = False` says the opposite thing about a class: none of its
# methods can wait, so an async wrapper would buy nothing and cost a
# thread hop in front of a substring read. Such a class crosses every
# layer as itself. The rule the codegen applies is: emit a wrapper when
# the object needs a home thread (affine) OR its methods can block.

from libcpp.string cimport string
from libcpp.vector cimport vector
from cython.operator cimport dereference as deref

from cythonix_bindings.c_mock_store cimport (
    CMockStore,
    CMockStorePath,
    CMockDerivation,
    CMockDerivedPath,
    CMockLocalStore,
    CMockRemoteStore,
    describe_store,
)

# --- Trampoline: C++ class that forwards virtuals to Python ---
cdef extern from *:
    """
    #include "fake_library/store.hpp"
    #include <Python.h>
    #include <string>

    // Lives only in the binding. Lets Python classes that inherit from
    // MockStore override get_uri() and have C++ code see the override.
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
    cdef cppclass PyStore(CMockStore):
        PyStore(object self)


# --- One wrapper class; every method declared once ---
# Allocation rules (same reasoning as any Cython binding of abstract C++):
# - A base __cinit__ ALWAYS runs before the leaf's and cannot be skipped,
#   so the base must not allocate here. Leaves allocate their own C++
#   object in their own __cinit__; the base stays leaf-agnostic.
# - Python subclasses of MockStore have no allocating __cinit__, so _ptr is
#   still NULL by __init__ time; the base __init__ then installs the
#   trampoline. Dealloc and init paths are NULL-guarded.
cdef class MockStore:
    cdef CMockStore* _ptr

    _threading = "pool"
    # Abstract, and GENERATED. MockStore is the type real callers hold most
    # of the time - you ask for a store and use it without caring which
    # implementation answered - so it needs an async wrapper and a wire
    # identity of its own. What it does not need is construction: a bare
    # MockStore() is a trampoline whose get_uri calls a Python method that
    # does not exist. Subclasses construct; this one is a base.
    _abstract = True
    # The C++ declaration this class binds. Declared per class, never
    # inherited: the codegen reads __dict__, so MockLocalStore must name its
    # own. This is the ONLY link between the pxd and the pyx.
    _binds = "CMockStore"

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

    def is_valid_path(self, MockStorePath path) -> bool:
        return self._ptr.is_valid_path(deref(path._ptr))

    def query_all_valid_paths(self) -> list[MockStorePath]:
        """Every path this store holds.

        The first method to hand back a LIST of anything, which is a
        repeated field on the wire. MockStorePath is a wire value, so
        each element crosses as its own message rather than as a
        handle - a list of proxies is refused, because nothing grants
        leases in bulk."""
        cdef vector[CMockStorePath] found
        cdef size_t i
        cdef MockStorePath path
        with nogil:
            found = self._ptr.query_all_valid_paths()
        out = []
        for i in range(found.size()):
            path = MockStorePath.__new__(MockStorePath)
            path._ptr = new CMockStorePath(found[i])
            out.append(path)
        return out

    def add_text_to_store(self, str name, str contents) -> MockStorePath:
        cdef string c_name = name.encode('utf-8')
        cdef string c_contents = contents.encode('utf-8')
        cdef MockStorePath result = MockStorePath.__new__(MockStorePath)
        with nogil:
            result._ptr = new CMockStorePath(self._ptr.add_text_to_store(c_name, c_contents))
        return result

    def build_derivation(self, MockDerivedPath request) -> MockStorePath:
        cdef CMockDerivedPath* c_req = request._ptr
        cdef MockStorePath result = MockStorePath.__new__(MockStorePath)
        with nogil:
            result._ptr = new CMockStorePath(self._ptr.build_derivation(deref(c_req)))
        return result

    def query_derivation(self, MockStorePath drv_path) -> MockDerivation:
        """Parse a .drv previously added to this store. The returned
        derivation carries mutable state: callers must keep invoking it
        on this store's thread (the async layer enforces that)."""
        cdef MockDerivation drv = MockDerivation.__new__(MockDerivation)
        with nogil:
            drv._ptr = new CMockDerivation(self._ptr.query_derivation(deref(drv_path._ptr)))
        return drv


# --- Concrete subclasses ---
# Each leaf allocates its own C++ object in its own __cinit__. The base
# contributes nothing to the chain.

cdef class MockLocalStore(MockStore):
    _threading = "pool"
    _binds = "CMockLocalStore"

    def __cinit__(self):
        self._ptr = new CMockLocalStore()


cdef class MockRemoteStore(MockStore):
    _threading = "affine"
    _binds = "CMockRemoteStore"

    def __cinit__(self):
        self._ptr = new CMockRemoteStore()


# --- Value types ---
# Never constructed by Python directly: instances come from store methods
# via __new__ (which skips __init__). The _threading marker tells the
# codegen how returned instances may be accessed.

cdef class MockStorePath:
    _threading = "pool"
    # Immutable value: safe to serialize across a wire, so it crosses
    # wrapper boundaries as a copy.
    _wire = "value"
    _binds = "CMockStorePath"
    # Nothing here allocates, does IO or waits: every accessor reads a
    # substring of the one string this object holds. So there is no
    # thread to hop to and no GIL to release, and the codegen emits no
    # async wrapper - a MockStorePath is handed back as itself, on both
    # sides of the wire (tasks/025).
    _blocking = False
    # Serialization contract for every wire-value type, read by the
    # codegen. The field list IS the proto message shape; a field type
    # naming another wire-value nests that type's message. _parts()
    # returns the values in this order and _from_parts() rebuilds from
    # them, so no layer above this file knows what a MockStorePath contains.
    _wire_fields = (("base_name", "str"),)

    cdef CMockStorePath* _ptr

    def __init__(self):
        raise TypeError("MockStorePath instances are produced by stores, not constructed")

    def __dealloc__(self):
        del self._ptr

    def __copy__(self):
        cdef MockStorePath c = MockStorePath.__new__(MockStorePath)
        c._ptr = new CMockStorePath(deref(self._ptr))
        return c

    def __deepcopy__(self, memo):
        # Immutable: deep copy == copy.
        return self.__copy__()

    def to_string(self) -> str:
        return self._ptr.to_string().decode('utf-8')

    def hash_part(self) -> str:
        return self._ptr.hash().decode('utf-8')

    @classmethod
    def _from_parts(cls, str base_name):
        """Wire-deserialization helper (private, never surfaced by the
        codegen): rebuild a produced value from its '<hash>-<name>'.
        Parsing and validation live in C++, mirroring real Nix."""
        cdef MockStorePath s = MockStorePath.__new__(MockStorePath)
        s._ptr = new CMockStorePath(base_name.encode('utf-8'))
        return s

    def _parts(self):
        """Wire-serialization helper (private): one value per
        _wire_fields entry, in order."""
        return (self._ptr.to_string().decode('utf-8'),)

    def name_part(self) -> str:
        return self._ptr.name().decode('utf-8')


cdef class MockDerivation:
    _threading = "affine"
    _binds = "CMockDerivation"
    # The instructive wire case: looks like a value, but set_env and the
    # access counter mutate it - so despite being a plain data holder it
    # must travel as a proxy. Mutability forces proxy, always.
    _wire = "proxy"

    cdef CMockDerivation* _ptr

    def __init__(self):
        raise TypeError("MockDerivation instances come from query_derivation")

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


cdef class MockDerivedPath:
    _threading = "pool"
    # Immutable build request: wire-value.
    _wire = "value"
    _binds = "CMockDerivedPath"
    # Same as MockStorePath: a self-contained request object whose only
    # method formats its own fields. No wrapper.
    _blocking = False
    # A wire-value field may name another wire-value type: the emitted
    # message nests MockStorePathMsg and the codec recurses into it.
    # A trailing "?" marks an optional field: proto3 cannot tell an
    # unset string from an empty one, so the contract says which way to
    # read it back. Opaque requests carry no output.
    _wire_fields = (("path", "MockStorePath"), ("output", "str?"))

    cdef CMockDerivedPath* _ptr

    def __cinit__(self, MockStorePath path=None, str output=None):
        # Optional args exist only so __copy__ can allocate via __new__
        # (tp_new always runs __cinit__); real construction validates
        # in __init__ below.
        if path is None:
            self._ptr = NULL
            return
        if output is None:
            self._ptr = new CMockDerivedPath(deref((<MockStorePath>path)._ptr))
        else:
            self._ptr = new CMockDerivedPath(deref((<MockStorePath>path)._ptr), output.encode('utf-8'))

    def __init__(self, MockStorePath path=None, str output=None):
        if path is None:
            raise TypeError("MockDerivedPath requires a MockStorePath")

    def __dealloc__(self):
        if self._ptr != NULL:
            del self._ptr

    def __copy__(self):
        cdef MockDerivedPath c = MockDerivedPath.__new__(MockDerivedPath)
        c._ptr = new CMockDerivedPath(deref(self._ptr))
        return c

    def __deepcopy__(self, memo):
        # Immutable: deep copy == copy.
        return self.__copy__()

    def describe(self) -> str:
        return self._ptr.describe().decode('utf-8')

    @classmethod
    def _from_parts(cls, MockStorePath path, str output):
        """Wire-deserialization helper (private)."""
        cdef MockDerivedPath d = MockDerivedPath.__new__(MockDerivedPath)
        if output is None:
            d._ptr = new CMockDerivedPath(deref(path._ptr))
        else:
            d._ptr = new CMockDerivedPath(deref(path._ptr), output.encode('utf-8'))
        return d

    def _parts(self):
        """Wire-serialization helper (private): (MockStorePath, output|None).
        The first element is a real MockStorePath, matching the declared
        _wire_fields type - the codec serializes it in turn."""
        cdef str out = None
        if self._ptr.is_built():
            out = self._ptr.output_name().decode('utf-8')
        cdef MockStorePath p = MockStorePath.__new__(MockStorePath)
        p._ptr = new CMockStorePath(self._ptr.path())
        return (p, out)


def describe(obj) -> str:
    """C++ free function describe_store(const MockStore&) - goes through C++
    virtual dispatch. Sees get_uri overrides on trampoline subclasses
    (Python subclasses of MockStore), not on plain-Python overrides of
    MockLocalStore/MockRemoteStore."""
    if not isinstance(obj, MockStore):
        raise TypeError(f"describe() expects a MockStore, got {type(obj)}")
    cdef string res = describe_store(deref((<MockStore>obj)._ptr))
    return res.decode('utf-8')


# Module-level functions carry the same marker their classes do, and it
# is what OPTS THEM IN: the codegen wraps only what is marked, so a
# helper the module happens to export stays out of the surface.
#
# "pool" is the only policy available. A free function has no instance
# and therefore no home thread to be affine to; the codegen rejects
# anything else.
describe._threading = "pool"
# Same marker a class carries, for the same reason: the Python name and
# the pxd name differ, and nothing else joins them.
describe._binds = "describe_store"
