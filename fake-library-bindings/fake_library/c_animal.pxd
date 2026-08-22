# cython: language_level=3
# This is the "pxd" that declares the C++ API.
# Think of it as a Cython header — it tells Cython what C++ classes exist.

from libcpp.string cimport string

cdef extern from "fake_library/animal.hpp" namespace "fake_library":
    cdef cppclass Animal:
        Animal(string name)
        string get_name() const
        void set_name(const string& name)
        string speak() const
        int legs() const
        # except + converts any C++ exception thrown here into a Python exception
        string fetch(const string& item) except +

    cdef cppclass Cat(Animal):
        Cat(string name)

    cdef cppclass Dog(Animal):
        Dog(string name)

    string describe_animal(const Animal& animal)
