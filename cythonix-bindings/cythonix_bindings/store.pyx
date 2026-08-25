# cython: language_level=3
# cython: annotation_typing=False
# The real nix::Store (tasks/015).
#
# Abstract in C++, and its implementation is chosen by a URI, so the
# binding is constructed through openStore rather than a constructor.
# "dummy://" is an in-memory store and needs nothing on disk, which is
# what makes this testable in a build sandbox.

from cython.operator cimport dereference as deref
from libcpp.memory cimport shared_ptr
from libcpp.string cimport string
from libcpp.vector cimport vector

from cythonix_bindings.c_path cimport CStorePath
from cythonix_bindings.c_store cimport (
    CStore,
    add_path_to_store,
    add_to_store,
    init_libstore,
    open_store,
    parse_store_path,
    query_all_valid_paths,
    store_uri,
)
from cythonix_bindings.path cimport StorePath

# The vocabularies libstore parses, as types. A StrEnum member IS the
# string, so this is the same call either way - it exists so an editor
# can offer the options and a typo fails before the call.
from cythonix_bindings.content_address import (
    ContentAddressMethod,
    HashAlgorithm,
)

# Before anything reaches libstore. Importing this module is the first
# moment that can happen, and libstore aborts rather than raises if it
# has not - so there is no later point that would still be safe.
init_libstore()


cdef class Store:
    """A real nix::Store, opened from a URI.

    `Store("dummy://")` is in-memory. `Store("auto")` is whatever the
    ambient configuration says, which usually means the daemon."""

    # A store talks to a daemon or a database, so its calls block and
    # it needs a thread to hop off the loop onto. Nix stores carry
    # their own locking, so any pool thread will do.
    _threading = "pool"
    _binds = "CStore"
    # Identity matters and it is not serializable: a store is a
    # connection, not a value.
    _wire = "proxy"
    # There is no constructor to read a signature from - nix::Store is
    # abstract. openStore is the factory, and its parameters are the
    # ones a caller passes.
    _ctor_from = "open_store"

    # shared_ptr, not a raw pointer: openStore hands back a ref<Store>,
    # which is a shared_ptr that cannot be null, and the store has to
    # stay alive as long as this wrapper does.
    cdef shared_ptr[CStore] _store

    def __init__(self, str uri):
        cdef string c_uri = uri.encode('utf-8')
        with nogil:
            self._store = open_store(c_uri)

    cdef inline CStore* _get(self) except NULL:
        if not self._store:
            raise ValueError("this Store was never opened")
        return self._store.get()

    def get_uri(self) -> str:
        """How this store describes itself.

        For logging only, upstream is explicit about that: it does not
        round-trip as a store reference and it is not a cache key."""
        return store_uri(deref(self._get())).decode('utf-8')

    def is_valid_path(self, StorePath path) -> bint:
        """Whether the store has that path."""
        cdef CStore* store = self._get()
        cdef CStorePath* p = path._get()
        cdef bint out
        with nogil:
            out = store.is_valid_path(deref(p))
        return out

    def add_to_store(self, name: str, data: bytes,
                     method: ContentAddressMethod = ContentAddressMethod.NAR,
                     hash_algo: HashAlgorithm = HashAlgorithm.SHA256
                     ) -> StorePath:
        """Add one file's contents to the store, and name the result.

        `data` is a regular file's CONTENTS - bytes, not text, because
        a store holds files and the hash that names the path is a hash
        of exactly these bytes.

        `method` and `hash_algo` are Nix's own words, parsed by Nix.
        They are StrEnums, so a member IS the string and a plain
        "flat" is the same call - the types exist so an editor offers
        the options. A word libstore does not know still raises with
        libstore's message rather than being corrected here, and one
        it knows but has gated says so instead: `git` and `blake3` are
        experimental features, and "disabled" is a different answer
        from "unknown".

        Both defaults are libstore's own, read off addToStoreFromDump:
        `hashMethod = NixArchive` and `hashAlgo = SHA256`. They are not
        a judgement made here, so a caller who omits them gets what Nix
        itself would have done - which is also what `nix-store --add`
        does.

        `nar` and a flat dump are not in conflict. The dump says how
        these bytes arrive, and this binding takes a file's contents,
        so it is always flat; the method says how the hash that names
        the path is computed, and Nix serialises the file into a NAR to
        compute it.

        Annotated Python-style, not Cython-style: this is backed by a
        shim rather than by a method on nix::Store, so there is no pxd
        declaration to backfill the types from and a `str name` would
        reach the codegen as Any."""
        cdef string c_name = name.encode('utf-8')
        cdef string c_data = data
        cdef string c_method = method.encode('utf-8')
        cdef string c_algo = hash_algo.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = add_to_store(deref(store), c_name, c_data, c_method, c_algo)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp

    def add_path_to_store(self, name: str, path: str,
                          method: ContentAddressMethod = ContentAddressMethod.NAR,
                          hash_algo: HashAlgorithm = HashAlgorithm.SHA256
                          ) -> StorePath:
        """Add a file or a directory from the filesystem to the store.

        The other half of `add_to_store`. That one takes a regular
        file's contents; this one takes a path and reads it, which is
        the only way to add a DIRECTORY - a directory has no contents
        to hand over as bytes, and `nar` is the only method that can
        describe one.

        `path` names a file on the filesystem THE STORE READS. In
        process that is this machine. Over RPC it is the server's, and
        no client path is sent: the argument crosses as the string it
        is, and libstore opens it on the far side. That is libstore's
        own meaning, not a limit added here - `add_to_store` is the
        call that carries bytes across.

        The defaults are libstore's, read off this overload of
        addToStore: `nar` and `sha256`, the same pair `nix-store --add`
        uses.

        A missing path fails with libstore's message. The shim
        canonicalises weakly, which upstream asks for and which does
        not require the path to exist - so the error comes from the
        layer that knows what it was for."""
        cdef string c_name = name.encode('utf-8')
        cdef string c_path = path.encode('utf-8')
        cdef string c_method = method.encode('utf-8')
        cdef string c_algo = hash_algo.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = add_path_to_store(
                deref(store), c_name, c_path, c_method, c_algo)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp

    def query_all_valid_paths(self) -> list[StorePath]:
        """Every path this store holds.

        Not every store answers it. nix::Store's own implementation
        raises "not supported by store", and only the local and remote
        stores override it - which is honest, because a substituter has
        no such list to give.

        Each element arrives as a pointer this binding owns, so the
        loop hands ownership to a wrapper and blanks the slot. Whatever
        never reached a wrapper is freed on the way out."""
        cdef CStore* store = self._get()
        cdef vector[CStorePath *] found
        cdef CStorePath* leftover
        cdef size_t i
        cdef StorePath path
        with nogil:
            found = query_all_valid_paths(deref(store))
        out = []
        try:
            for i in range(found.size()):
                path = StorePath.__new__(StorePath)
                path._ptr = found[i]
                found[i] = NULL
                out.append(path)
        finally:
            for i in range(found.size()):
                leftover = found[i]
                if leftover is not NULL:
                    del leftover
        return out

    def print_store_path(self, StorePath path) -> str:
        """The path as an absolute filesystem path in this store."""
        cdef CStore* store = self._get()
        cdef CStorePath* p = path._get()
        cdef string out
        with nogil:
            out = store.print_store_path(deref(p))
        return out.decode('utf-8')

    def parse_store_path(self, path: str) -> StorePath:
        """An absolute path in this store, as a StorePath.

        Raises when it is not in this store's directory - which is a
        different question from whether the name is well formed, and
        the reason this lives on the store rather than on StorePath."""
        cdef string c_path = path.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = parse_store_path(deref(store), c_path)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp
