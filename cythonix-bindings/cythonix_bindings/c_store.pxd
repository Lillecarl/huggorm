# cython: language_level=3
# Declaration of the REAL Nix store API: include/nix/store/store-api.hh.
#
# nix::Store is abstract and its implementation is chosen by a URI, so
# there is no constructor to declare. openStore is the factory, and the
# binding says so with _ctor_from.

from libcpp.memory cimport shared_ptr
from libcpp.string cimport string
from libcpp.vector cimport vector

from cythonix_bindings.c_path cimport CStorePath, translate_nix_error


cdef extern from "nix/store/store-api.hh" nogil:
    cdef cppclass CStore "nix::Store":
        # Both of these can talk to a daemon, so both can block.
        bint is_valid_path "isValidPath" (const CStorePath & path) except +translate_nix_error
        string print_store_path "printStorePath" (const CStorePath & path) except +translate_nix_error


cdef extern from "cythonix_bindings/_cpp/store.hpp" namespace "cythonix" nogil:
    # Must run before anything else in libstore. It does not raise when
    # it has not: it aborts the process.
    void init_libstore()
    # The factory. Its parameters ARE the constructor's, which is what
    # _ctor_from on the binding means.
    shared_ptr[CStore] open_store(string uri) except +translate_nix_error
    string store_uri(const CStore & store) except +translate_nix_error
    # Returns a pointer because nix::StorePath is not
    # default-constructible; see _cpp/store.hpp.
    CStorePath * parse_store_path(const CStore & store, string path) except +translate_nix_error
    # A vector of POINTERS for the same reason, one level down: the
    # binding owns every element it takes out.
    vector[CStorePath *] query_all_valid_paths(CStore & store) except +translate_nix_error
    # The enums arrive as the strings Nix parses, so the vocabulary -
    # and the error for a wrong one - stays Nix's.
    CStorePath * add_to_store(CStore & store, string name, string data, string method, string hash_algo) except +translate_nix_error
