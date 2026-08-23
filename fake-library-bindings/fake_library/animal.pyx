# cython: language_level=3
# cython: annotation_typing=False
# This is the "pyx" — the implementation that bridges Python <-> C++.
#
# Layout:
# - PyAnimal (trampoline, verbatim C++ via extern-from-*): forwards virtual
#   calls to Python so subclasses of Animal are visible to C++ dispatch.
# - ONE cdef class Animal holds Animal* and declares every wrapped method
#   exactly once. Cat/Dog are constructor-only subclasses: they just pick
#   which C++ object to allocate; all methods inherit through the C++ vtable.
# - Leaves allocate their own C++ object in their own __cinit__
#   (see Cat/Dog); the base stays leaf-agnostic.

from libcpp.string cimport string
from cython.operator cimport dereference as deref

# The C++ API is declared in c_animal.pxd (cimported below). That file is
# the single hand-written declaration surface: the compiler validates our
# usage against it, and the codegen parses it (with Cython's own parser)
# to learn names, params and return types for spec generation.
from fake_library.c_animal cimport (
    CAnimal,
    CBall,
    CCat,
    CDog,
    CPoop,
    describe_animal,
)

# --- Trampoline: C++ class that forwards virtuals to Python ---
cdef extern from *:
    """
    #include "fake_library/animal.hpp"
    #include <Python.h>
    #include <string>

    // Lives only in the binding. Lets Python classes that inherit from
    // Animal override speak()/legs() and have C++ code see the override.
    struct PyAnimal : public fake_library::Animal {
        PyObject* py_self;
        PyAnimal(std::string name, PyObject* self)
            : fake_library::Animal(std::move(name)), py_self(self) {
            Py_INCREF(py_self);
        }
        ~PyAnimal() {
            PyGILState_STATE g = PyGILState_Ensure();
            Py_DECREF(py_self);
            PyGILState_Release(g);
        }
        std::string speak() const override {
            PyGILState_STATE g = PyGILState_Ensure();
            PyObject* res = PyObject_CallMethod(py_self, "speak", NULL);
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
        int legs() const override {
            PyGILState_STATE g = PyGILState_Ensure();
            PyObject* res = PyObject_CallMethod(py_self, "legs", NULL);
            int out = 0;
            if (res) {
                out = PyLong_AsLong(res);
                Py_DECREF(res);
            } else {
                PyErr_Clear();
            }
            PyGILState_Release(g);
            return out;
        }
    };
    """
    cdef cppclass PyAnimal(CAnimal):
        PyAnimal(string name, object self)


# --- One wrapper class; every method declared once ---
# Allocation rules (docs/userguide/special_methods.rst):
# - A base __cinit__ ALWAYS runs before the leaf's and can be neither
#   skipped nor called explicitly. So the base must not allocate here:
#   any leaf would overwrite it (leak + pinned self-ref), and avoiding
#   that via type(self) switches would couple the base to its leaves.
# - Instead: leaves allocate their own C++ object in their own
#   __cinit__ (see Cat/Dog). The base stays leaf-agnostic.
# - Python subclasses of Animal have no allocating __cinit__ anywhere in
#   their chain, so _ptr is still NULL by __init__ time; the base's
#   __init__ then installs the trampoline. This is why the dealloc and
#   init paths are NULL-guarded.
cdef class Animal:
    cdef CAnimal* _ptr

    _threading = "affine"
    _async = False  # abstract: excluded from wrapper generation

    def __init__(self, str name=""):
        cdef string c_name
        if self._ptr == NULL:
            c_name = name.encode('utf-8')
            self._ptr = new PyAnimal(c_name, <object>self)

    def __dealloc__(self):
        if self._ptr != NULL:
            del self._ptr

    # GIL policy is decided PER METHOD here — only the binding knows
    # whether a C++ implementation may re-enter Python or how long it
    # runs:
    # - speak()/legs(): KEEP the GIL. For trampoline-backed instances
    #   (Python subclasses) they re-enter Python via PyAnimal; for
    #   concrete ones they are sub-microsecond.
    # - fetch/wait_ms: RELEASE. Potentially slow, pure C++ after the
    #   arguments are converted.
    def speak(self) -> str:
        return self._ptr.speak().decode('utf-8')

    def legs(self) -> int:
        return self._ptr.legs()

    def fetch(self, str item) -> str:
        # except + inside nogil reacquires the GIL only if a C++
        # exception actually throws.
        cdef string c_item
        cdef string result
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        c_item = item.encode('utf-8')
        with nogil:
            result = self._ptr.fetch(c_item)
        return result.decode('utf-8')

    def wait_ms(self, int ms) -> None:
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        with nogil:
            self._ptr.wait_ms(ms)

    def poop(self) -> Poop:
        """Adopt a C++-created Poop. The wrapper is created here but the
        C++ object was born on whatever thread runs this method — callers
        must keep invoking it there (the async layer enforces this)."""
        cdef Poop p = Poop.__new__(Poop)
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        with nogil:
            p._ptr = new CPoop(self._ptr.poop())
        return p

    def toy(self) -> Ball:
        cdef Ball b = Ball.__new__(Ball)
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        with nogil:
            b._ptr = new CBall(self._ptr.toy())
        return b

    @property
    def name(self) -> str:
        cdef string n
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        with nogil:
            n = self._ptr.get_name()
        return n.decode('utf-8')

    @name.setter
    def name(self, str value):
        cdef string c_val
        if self._ptr == NULL:
            raise RuntimeError("animal not initialized")
        c_val = value.encode('utf-8')
        with nogil:
            self._ptr.set_name(c_val)

    def __repr__(self):
        return f"{type(self).__name__}(name={self.name!r})"


# --- Concrete subclasses ---
# Each leaf allocates its own C++ object in its own __cinit__. The base
# contributes nothing to the chain (see the note above Animal).
#
# `_threading` marks the execution policy the async generator uses:
#   "affine" - not thread-safe; ops pinned to one dedicated thread
#   "pool"   - safe on any thread
# `_async = False` excludes a class from wrapper generation (Animal:
# abstract).

cdef class Cat(Animal):
    _threading = "affine"

    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new CCat(c_name)


cdef class Dog(Animal):
    # Thread-safe exemplar so both policies have a concrete wrapper.
    _threading = "pool"

    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new CDog(c_name)


# --- Adopted value types ---
# Never constructed by Python directly: instances come from Animal methods
# via __new__ (which skips __init__). The _threading marker tells the
# codegen how returned instances may be accessed:
#   "affine" - mutable state; ops must run on the producer's thread
#   "pool"   - safe on any thread

cdef class Poop:
    _threading = "affine"

    cdef CPoop* _ptr

    def __init__(self):
        raise TypeError("Poop instances are produced by animals, not constructed")

    def __dealloc__(self):
        del self._ptr

    def describe(self) -> str:
        cdef string d
        if self._ptr == NULL:
            raise RuntimeError("poop not initialized")
        with nogil:
            d = self._ptr.describe()
        return d.decode('utf-8')

    def inspections(self) -> int:
        if self._ptr == NULL:
            raise RuntimeError("poop not initialized")
        return self._ptr.inspections()


cdef class Ball:
    _threading = "pool"

    cdef CBall* _ptr

    def __init__(self):
        raise TypeError("Ball instances are produced by animals, not constructed")

    def __dealloc__(self):
        if self._ptr == NULL:
            return
        del self._ptr

    def describe(self) -> str:
        cdef string d
        if self._ptr == NULL:
            raise RuntimeError("ball not initialized")
        with nogil:
            d = self._ptr.describe()
        return d.decode('utf-8')


def describe(obj) -> str:
    """C++ free function describe_animal(const Animal&) — goes through C++
    virtual dispatch. Sees overrides on trampoline subclasses (Python
    subclasses of Animal), not on plain-Python overrides of Cat/Dog."""
    if not isinstance(obj, Animal):
        raise TypeError(f"describe() expects an Animal, got {type(obj)}")
    cdef string res = describe_animal(deref((<Animal>obj)._ptr))
    return res.decode('utf-8')
