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
    cdef cppclass CStorePath "fake_library::StorePath":
        CStorePath() except +
        CStorePath(string hash, string name) except +
        CStorePath(string base_name) except +
        CStorePath(const CStorePath & other)
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
        CDerivedPath(CStorePath path)
        CDerivedPath(CStorePath drv_path, string output)
        CDerivedPath(const CDerivedPath & other)
        string describe() const
        bint is_built() const
        const CStorePath& path() const
        const string& output_name() const

    cdef cppclass CStore "fake_library::Store":
        string get_uri() const
        bint is_valid_path(const CStorePath & path) const
        CStorePath add_text_to_store(string name, string contents) except + nogil
        CStorePath build_derivation(const CDerivedPath & request) except + nogil
        CDerivation query_derivation(const CStorePath & drv_path) except + nogil

    cdef cppclass CLocalStore "fake_library::LocalStore" (CStore):
        pass

    cdef cppclass CRemoteStore "fake_library::RemoteStore" (CStore):
        pass

    string describe_store "fake_library::describe_store" (const CStore & store)
