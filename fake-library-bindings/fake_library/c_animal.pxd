# Single hand-written source of truth for the fake-library C++ surface.
#
# Consumed by two parties:
# - animal.pyx via cimport (the compiler cross-checks every use)
# - the codegen generator, which parses this file with Cython's own
#   parser (Cython.Compiler.Parsing.p_module with in_pxd=True) to learn
#   names/params/return types without any runtime introspection limits
#   and without regex.
#
# Conventions the generator relies on:
# - Cython-side class names are C-prefixed; the quoted cname carries the
#   real, fully-qualified C++ name
# - inheritance is declared with the C-prefixed base
# - `nogil` on the extern block makes all methods callable inside
#   `with nogil:` blocks; `except +` marks throwing methods

from libcpp.string cimport string

cdef extern from "fake_library/animal.hpp" nogil:
    cdef cppclass CAnimal "fake_library::Animal":
        CAnimal(string name)
        string get_name() const
        void set_name(const string& name)
        string speak() const
        int legs() const
        string fetch(const string& item) except +
        CPoop poop() const
        CBall toy() const
        void wait_ms(int ms) const

    cdef cppclass CCat "fake_library::Cat"(CAnimal):
        CCat(string name)

    cdef cppclass CDog "fake_library::Dog"(CAnimal):
        CDog(string name)

    cdef cppclass CPoop "fake_library::Poop":
        CPoop(string producer)
        CPoop(const CPoop& other)
        string describe()
        int inspections() const

    cdef cppclass CBall "fake_library::Ball":
        CBall(string color)
        CBall(const CBall& other)
        string describe() const

    string describe_animal(const CAnimal& animal)
