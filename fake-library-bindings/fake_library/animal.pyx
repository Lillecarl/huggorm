# cython: language_level=3
# This is the "pyx" — the implementation that bridges Python <-> C++.
#
# Layout:
# - PyAnimal (trampoline, verbatim C++ via extern-from-*): forwards virtual
#   calls to Python so subclasses of Animal are visible to C++ dispatch.
# - ONE cdef class Animal holds Animal* and declares every wrapped method
#   exactly once. Cat/Dog are constructor-only subclasses: they just pick
#   which C++ object to allocate; all methods inherit through the C++ vtable.
# - Cython calls only the most-derived __cinit__, so leaf classes allocate
#   their concrete pointer and never trigger the trampoline path.

from libcpp.string cimport string
from cython.operator cimport dereference as deref

# --- The C++ API, declared in place ---
# Cython cannot read fake_library/animal.hpp, so the API is described here
# in Cython syntax. This is the hand-written binding surface; it used to
# live in c_animal.pxd, but with a single module the pxd was pure
# indirection. Cython-side names get a C prefix to avoid clashing with the
# wrapper classes below; the quoted strings are the real C++ names.
cdef extern from "fake_library/animal.hpp":
    # Quoted names must be fully qualified: they are emitted verbatim
    # into generated C++ that lives outside the library's namespace.
    cdef cppclass CAnimal "fake_library::Animal":
        CAnimal(string name)
        string get_name() const
        void set_name(const string& name)
        string speak() const
        int legs() const
        # except + converts any C++ exception thrown here into a Python exception
        string fetch(const string& item) except +
        CPoop poop() const
        CBall toy() const

    cdef cppclass CCat "fake_library::Cat"(CAnimal):
        CCat(string name)

    cdef cppclass CDog "fake_library::Dog"(CAnimal):
        CDog(string name)

    cdef cppclass CPoop "fake_library::Poop":
        CPoop(string producer)
        string describe()
        int inspections() const

    cdef cppclass CBall "fake_library::Ball":
        CBall(string color)
        string describe() const

    string describe_animal(const CAnimal& animal)

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
cdef class Animal:
    cdef CAnimal* _ptr

    def __cinit__(self, str name=""):
        # Only reached when no __cinit__ override allocated a concrete
        # object — i.e. direct instantiation or a Python subclass.
        if type(self) is Animal:
            raise TypeError("Animal is abstract; subclass it and implement speak()/legs()")
        cdef string c_name = name.encode('utf-8')
        self._ptr = new PyAnimal(c_name, <object>self)

    def __dealloc__(self):
        del self._ptr

    # Virtual dispatch through Animal*. For plain wrappers this hits the
    # C++ impl; for Python subclasses _ptr is the PyAnimal trampoline,
    # which routes back to the Python override.
    def speak(self) -> str:
        return self._ptr.speak().decode('utf-8')

    def legs(self) -> int:
        return self._ptr.legs()

    def fetch(self, str item) -> str:
        # C++ exceptions convert via the `except +` above
        return self._ptr.fetch(item.encode('utf-8')).decode('utf-8')

    def poop(self) -> Poop:
        """Adopt a C++-created Poop. The wrapper is created here but the
        C++ object was born on whatever thread runs this method — callers
        must keep invoking it there (the async layer enforces this)."""
        cdef Poop p = Poop.__new__(Poop)
        p._ptr = new CPoop(self._ptr.get_name())
        # C++ returns Poop by value; move it into the wrapper's allocation.
        p._ptr[0] = self._ptr.poop()
        return p

    def toy(self) -> Ball:
        cdef Ball b = Ball.__new__(Ball)
        b._ptr = new CBall(self._ptr.get_name())
        b._ptr[0] = self._ptr.toy()
        return b

    @property
    def name(self) -> str:
        return self._ptr.get_name().decode('utf-8')

    @name.setter
    def name(self, str value):
        cdef string c_val = value.encode('utf-8')
        self._ptr.set_name(c_val)

    def __repr__(self):
        return f"{type(self).__name__}(name={self.name!r})"


# --- Constructor-only subclasses ---
# All methods inherited; only allocation differs. The base's trampoline
# __cinit__ is NOT called for these (Cython runs the most-derived one).

cdef class Cat(Animal):
    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new CCat(c_name)


cdef class Dog(Animal):
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
        return self._ptr.describe().decode('utf-8')

    def inspections(self) -> int:
        return self._ptr.inspections()


cdef class Ball:
    _threading = "pool"

    cdef CBall* _ptr

    def __init__(self):
        raise TypeError("Ball instances are produced by animals, not constructed")

    def __dealloc__(self):
        del self._ptr

    def describe(self) -> str:
        return self._ptr.describe().decode('utf-8')


def describe(obj) -> str:
    """C++ free function describe_animal(const Animal&) — goes through C++
    virtual dispatch. Sees overrides on trampoline subclasses (Python
    subclasses of Animal), not on plain-Python overrides of Cat/Dog."""
    if not isinstance(obj, Animal):
        raise TypeError(f"describe() expects an Animal, got {type(obj)}")
    cdef string res = describe_animal(deref((<Animal>obj)._ptr))
    return res.decode('utf-8')
