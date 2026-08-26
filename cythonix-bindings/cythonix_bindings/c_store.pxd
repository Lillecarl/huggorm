# cython: language_level=3
# Declaration of the REAL Nix store API: include/nix/store/store-api.hh.
#
# nix::Store is abstract and its implementation is chosen by a URI, so
# there is no constructor to declare. openStore is the factory, and the
# binding says so with _ctor_from.

from libc.stdint cimport int64_t, uint64_t
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
    # Which store path CONTAINS a file, which is a different question.
    # A POD for the same reason CPathInfo is one.
    cdef struct CStoreLocation "cythonix::StoreLocationParts":
        string path
        string sub_path
    CStoreLocation to_store_path(const CStore & store, string path) except +translate_nix_error
    # A vector of POINTERS for the same reason, one level down: the
    # binding owns every element it takes out.
    vector[CStorePath *] query_all_valid_paths(CStore & store) except +translate_nix_error
    # The enums arrive as the strings Nix parses, so the vocabulary -
    # and the error for a wrong one - stays Nix's.
    # `references` is what this path points AT. Nix is told them; it
    # does not scan for them. A vector of base names, because a pxd
    # cannot declare the std::set libstore takes.
    CStorePath * add_to_store(CStore & store, string name, string data, string method, string hash_algo, vector[string] references) except +translate_nix_error
    # The other overload: a path on the filesystem the store reads,
    # through a nix::SourcePath the shim builds.
    CStorePath * add_path_to_store(CStore & store, string name, string path, string method, string hash_algo, vector[string] references) except +translate_nix_error
    # Where the files really are. Only a LocalFSStore has an answer,
    # so the shim asks and refuses like libstore itself.
    string real_path(CStore & store, const CStorePath & path) except +translate_nix_error
    # A POD, so Cython can hold one by value. nix::ValidPathInfo cannot
    # be declared here at all - see _cpp/store.hpp for why the crossing
    # point is flattened.
    cdef struct CPathInfo "cythonix::PathInfoParts":
        string path
        string nar_hash
        uint64_t nar_size
        string deriver
        int64_t registration_time
        bint ultimate
        # Nix keeps both as sets; the shim flattens them to vectors,
        # which is what a pxd can declare. See _cpp/store.hpp.
        vector[string] references
        vector[string] sigs
    CPathInfo path_info(CStore & store, const CStorePath & path) except +translate_nix_error
