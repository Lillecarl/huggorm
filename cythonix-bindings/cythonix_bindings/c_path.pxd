# cython: language_level=3
# Declaration of the REAL Nix store API: include/nix/store/path.hh from
# the nix package's dev output (tasks/015).
#
# The mock's c_mock_store.pxd stands beside this one. Both are read by the
# codegen, one class binds nix::StorePath and another binds
# fake_library::StorePath, and the two coexist until the mock goes.
#
# 100% C++, by decision: the C API is not feature complete, so it is
# not a fallback for the awkward cases either.

from libcpp.string cimport string
from libcpp.string_view cimport string_view

# Every `except +translate_nix_error` below names this. A bare
# `except +` would map nix::BadStorePathName onto RuntimeError
# and leave libstore's terminal escape codes in the message.
cdef extern from "cythonix_bindings/_cpp/errors.hpp" namespace "cythonix" nogil:
    cdef void translate_nix_error()

cdef extern from "nix/store/path.hh" nogil:
    # nix::StorePath deletes its default constructor. That costs
    # nothing here: a binding reaches it through a pointer that starts
    # NULL and a factory assigns, never by default-constructing. What
    # WOULD cost something is declaring a default constructor Cython
    # could then emit a call to.
    cdef cppclass CStorePath "nix::StorePath":
        CStorePath(const CStorePath & other)
        # Throws nix::BadStorePath on a name that is not a store path.
        # Validation living in C++ is the whole reason to bind the real
        # thing rather than reimplement it.
        CStorePath(string base_name) except +translate_nix_error
        # Every accessor returns a view INTO the object's own string.
        # The binding copies before handing anything to Python: a view
        # outliving its owner is a dangling pointer, not an exception.
        string_view to_string() except +translate_nix_error
        string_view name() except +translate_nix_error
        string_view hash_part "hashPart" () except +translate_nix_error
        bint is_derivation "isDerivation" () except +translate_nix_error
