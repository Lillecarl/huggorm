# cython: language_level=3
# This pxd exposes the Cython extension types to other Cython modules.
# If fake-library-python were also Cython, it could `cimport` these.

cimport fake_library.c_animal as c_animal

cdef class Animal:
    cdef c_animal.Animal* _ptr
    # Allow subclasses to override; base holds generic pointer.
    # If Python subclass overrides speak/legs, the trampoline (PyAnimal) will forward.

cdef class Cat:
    cdef c_animal.Cat* _ptr

cdef class Dog:
    cdef c_animal.Dog* _ptr
