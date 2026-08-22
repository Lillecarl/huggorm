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
cimport fake_library.c_animal as c_animal
from cython.operator cimport dereference as deref

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
    cdef cppclass PyAnimal(c_animal.Animal):
        PyAnimal(string name, object self)


# --- One wrapper class; every method declared once ---
cdef class Animal:
    cdef c_animal.Animal* _ptr

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
        # C++ exceptions convert via `except +` in c_animal.pxd
        return self._ptr.fetch(item.encode('utf-8')).decode('utf-8')

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
        self._ptr = new c_animal.Cat(c_name)


cdef class Dog(Animal):
    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new c_animal.Dog(c_name)


def describe(obj) -> str:
    """C++ free function describe_animal(const Animal&) — goes through C++
    virtual dispatch. Sees overrides on trampoline subclasses (Python
    subclasses of Animal), not on plain-Python overrides of Cat/Dog."""
    if not isinstance(obj, Animal):
        raise TypeError(f"describe() expects an Animal, got {type(obj)}")
    cdef string res = c_animal.describe_animal(deref((<Animal>obj)._ptr))
    return res.decode('utf-8')
