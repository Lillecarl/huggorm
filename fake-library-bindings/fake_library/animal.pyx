# cython: language_level=3
# This is the "pyx" — the implementation that bridges Python <-> C++.
#
# Key ideas:
# - `cdef class` creates a Python-visible extension type that can hold C++ pointers
# - `__cinit__` / `__dealloc__` manage C++ lifetime (new/delete)
# - We translate std::string <-> Python str via .encode()/.decode()
# - These classes are sub-classable from Python: `class MyCat(Cat): ...`
# - For true C++ virtual dispatch, we need a trampoline (PyAnimal) that
#   forwards C++ virtual calls back to Python. That's what Animal below does.
#   Cat/Dog without trampoline show the limitation: Python override is
#   Python-only, not visible to C++.

from libcpp.string cimport string
cimport fake_library.c_animal as c_animal
from cython.operator cimport dereference as deref

# --- Trampoline: C++ class that forwards virtuals to Python ---
# This is the key to "Python subclass visible to C++".
# It holds a PyObject* to the Python instance and calls PyObject_CallMethod.
cdef extern from *:
    """
    #include "fake_library/animal.hpp"
    #include <Python.h>
    #include <string>

    // PyAnimal lives only in the binding, not in the library.
    // It lets Python classes that inherit from Animal override speak()/legs()
    // and have C++ code (like describe_animal) see the override.
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

# --- Abstract base with trampoline support ---
cdef class Animal:
    # Holds a pointer that is actually a PyAnimal* when instantiated via Python subclass
    def __cinit__(self, str name=""):
        # Prevent direct instantiation — must be subclassed in Python
        if type(self) is Animal:
            raise TypeError("Animal is abstract and cannot be instantiated directly; subclass it and implement speak() and legs()")
        cdef string c_name = name.encode('utf-8')
        self._ptr = new PyAnimal(c_name, <object>self)

    def __init__(self, str name=""):
        # Cython calls __cinit__ then __init__. We make __init__ a no-op so
        # Python subclasses can call super().__init__(name) without error.
        pass

    def __dealloc__(self):
        del self._ptr

    # These are the methods Python subclasses override.
    # They are not used directly by C++ (C++ calls through PyAnimal),
    # but defining them here makes the intent clear.
    def speak(self) -> str:
        raise NotImplementedError("subclass must implement speak()")

    def legs(self) -> int:
        raise NotImplementedError("subclass must implement legs()")

    @property
    def name(self) -> str:
        return self._ptr.get_name().decode('utf-8')

    @name.setter
    def name(self, str value):
        cdef string c_val = value.encode('utf-8')
        self._ptr.set_name(c_val)

    def __repr__(self):
        return f"Animal(name={self.name!r})"


def describe(obj) -> str:
    """
    Calls the C++ free function `describe_animal(const Animal&)` .
    This goes through C++ virtual dispatch, so for trampoline-based Animal
    subclasses it will see the Python override. For Cat/Dog it sees the C++ impl.
    """
    cdef string res
    if isinstance(obj, Animal):
        res = c_animal.describe_animal(deref((<Animal>obj)._ptr))
        return res.decode('utf-8')
    elif isinstance(obj, Cat):
        # Cast Cat* to Animal* for C++ call
        res = c_animal.describe_animal(deref(<c_animal.Animal*>((<Cat>obj)._ptr)))
        return res.decode('utf-8')
    elif isinstance(obj, Dog):
        res = c_animal.describe_animal(deref(<c_animal.Animal*>((<Dog>obj)._ptr)))
        return res.decode('utf-8')
    else:
        raise TypeError(f"describe() expects Animal, Cat or Dog, got {type(obj)}")

# --- Concrete wrappers (no trampoline — Python override is Python-only) ---

cdef class Cat:
    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new c_animal.Cat(c_name)

    def __dealloc__(self):
        del self._ptr

    def speak(self) -> str:
        return self._ptr.speak().decode('utf-8')

    def legs(self) -> int:
        return self._ptr.legs()

    @property
    def name(self) -> str:
        return self._ptr.get_name().decode('utf-8')

    @name.setter
    def name(self, str value):
        cdef string c_val = value.encode('utf-8')
        self._ptr.set_name(c_val)

    def __repr__(self):
        return f"Cat(name={self.name!r})"


cdef class Dog:
    def __cinit__(self, str name):
        cdef string c_name = name.encode('utf-8')
        self._ptr = new c_animal.Dog(c_name)

    def __dealloc__(self):
        del self._ptr

    def speak(self) -> str:
        return self._ptr.speak().decode('utf-8')

    def legs(self) -> int:
        return self._ptr.legs()

    @property
    def name(self) -> str:
        return self._ptr.get_name().decode('utf-8')

    @name.setter
    def name(self, str value):
        cdef string c_val = value.encode('utf-8')
        self._ptr.set_name(c_val)

    def __repr__(self):
        return f"Dog(name={self.name!r})"
