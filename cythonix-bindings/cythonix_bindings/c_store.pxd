# cython: language_level=3
# Declaration of the C++ store API (include/fake_library/store.hpp).
# This file is the single hand-written declaration surface: the compiler
# validates the pyx against it, and the codegen parses it to learn which
# classes are returned across the wrapper surface.
#
# `nogil` marks what may run without the GIL - the same per-method policy
# the pyx documents. `except +` propagates C++ exceptions.

from libcpp.string cimport string

cdef extern from "fake_library/store.hpp" nogil:
    cdef cppclass CMockStorePath "fake_library::StorePath":
        CMockStorePath() except +
        CMockStorePath(string hash, string name) except +
        CMockStorePath(string base_name) except +
        CMockStorePath(const CMockStorePath & other)
        string to_string() const
        string hash() const
        string name() const

    cdef cppclass CDerivation "fake_library::Derivation":
        CDerivation()
        CDerivation(string name)
        CDerivation(const CDerivation & other)
        void set_env(string key, string value)
        string describe()
        int queries() const

    cdef cppclass CDerivedPath "fake_library::DerivedPath":
        CDerivedPath(CMockStorePath path)
        CDerivedPath(CMockStorePath drv_path, string output)
        CDerivedPath(const CDerivedPath & other)
        string describe() const
        bint is_built() const
        const CMockStorePath& path() const
        const string& output_name() const

    cdef cppclass CStore "fake_library::Store":
        string get_uri() const
        bint is_valid_path(const CMockStorePath & path) const
        CMockStorePath add_text_to_store(string name, string contents) except + nogil
        CMockStorePath build_derivation(const CDerivedPath & request) except + nogil
        CDerivation query_derivation(const CMockStorePath & drv_path) except + nogil

    cdef cppclass CLocalStore "fake_library::LocalStore" (CStore):
        pass

    cdef cppclass CRemoteStore "fake_library::RemoteStore" (CStore):
        pass

    string describe_store "fake_library::describe_store" (const CStore & store)
