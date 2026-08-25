# cython: language_level=3
# cython: annotation_typing=False
# The real nix::Store (tasks/015).
#
# Abstract in C++, and its implementation is chosen by a URI, so the
# binding is constructed through openStore rather than a constructor.
# "dummy://" is an in-memory store and needs nothing on disk, which is
# what makes this testable in a build sandbox.

import pathlib

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
    CPathInfo,
    parse_store_path,
    path_info,
    query_all_valid_paths,
    real_path,
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


cdef class PathInfo:
    """What a store knows about one path it holds.

    A VALUE, not a handle: it is what the store said at the moment it
    was asked, so it crosses the wire as a copy and nothing about it
    can go stale in a way a caller could act on.

    Produced, never constructed. Every field comes from the store's
    own database, so there is nothing a caller could correctly build
    one from - which `_produced` says out loud rather than leaving to
    an inference elsewhere."""

    _threading = "pool"
    # Six reads of memory this object already owns. Nothing blocks, so
    # there is no thread to hop to and the codegen emits no async
    # wrapper: a PathInfo is handed back as itself on both sides.
    _blocking = False
    _wire = "value"
    # Its __init__ raises, so no surface may offer a constructor.
    # Declared rather than inferred: today a produced class is one the
    # pxd names as a method return type, which is a proxy for this
    # fact and true of the others by coincidence.
    _produced = True
    # No _binds. nix::ValidPathInfo has no pxd spelling - see
    # _cpp/store.hpp - so this class binds the flattened struct rather
    # than the C++ type, and there is no declaration to link to.
    #
    # `deriver` is optional and says so with `?`: most paths have one
    # and a path added straight to the store has none.
    _wire_fields = (
        ("path", "StorePath"),
        ("nar_hash", "str"),
        ("nar_size", "int"),
        ("deriver", "StorePath?"),
        ("registration_time", "int"),
        ("ultimate", "bool"),
    )

    cdef object _path
    cdef object _nar_hash
    cdef object _nar_size
    cdef object _deriver
    cdef object _registration_time
    cdef object _ultimate

    def __init__(self):
        raise TypeError(
            "PathInfo objects come from Store.query_path_info, not from a "
            "constructor")

    def path(self) -> StorePath:
        """The path this describes."""
        return self._path

    def nar_hash(self) -> str:
        """The hash of the path's NAR serialisation, algorithm first:
        `sha256:<base32>`, the same spelling `nix path-info` prints."""
        return self._nar_hash

    def nar_size(self) -> int:
        """The size of that NAR in bytes. Not the size on disk."""
        return self._nar_size

    def deriver(self) -> StorePath:
        """The .drv that built this, or None.

        None is a real answer, not a gap: a path added straight to the
        store was not built by anything."""
        return self._deriver

    def registration_time(self) -> int:
        """When the store learnt about this path, as a Unix time."""
        return self._registration_time

    def ultimate(self) -> bint:
        """Whether this store built it itself, as opposed to receiving
        it from a substituter or an import."""
        return self._ultimate

    @classmethod
    def _from_parts(cls, path, nar_hash, nar_size, deriver,
                    registration_time, ultimate):
        """Wire-deserialization helper (private, never surfaced)."""
        cdef PathInfo info = PathInfo.__new__(PathInfo)
        info._path = path
        info._nar_hash = nar_hash
        info._nar_size = nar_size
        info._deriver = deriver
        info._registration_time = registration_time
        info._ultimate = ultimate
        return info

    def _parts(self):
        """Wire-serialization helper (private): one value per
        _wire_fields entry, in order."""
        return (self._path, self._nar_hash, self._nar_size, self._deriver,
                self._registration_time, self._ultimate)


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

    def real_path(self, path: StorePath) -> pathlib.Path:
        """Where this store object's files really are.

        A different question from `print_store_path`, which joins the
        store DIRECTORY onto the path. A chroot store keeps /nix/store
        as its store directory and puts the files under <root>/nix/store,
        so its printed path does not exist and this one does.

        Not every store has an answer. libstore puts toRealPath on
        LocalFSStore rather than on Store, because a binary cache or an
        ssh-ng store has no directory on this filesystem - so this
        raises Unsupported for one that does not, the same way
        query_all_valid_paths does.

        A pathlib.Path, so the result is a thing to open and walk
        rather than a string to join by hand. It is a path on the
        machine the STORE runs on: in process that is this one, and
        over RPC it is the server's (tasks/040).

        `path` is annotated Python-style, so the codegen can read the
        type: this method is backed by a shim rather than declared on
        nix::Store, so there is no pxd method to backfill from. A
        Cython-style `StorePath path` would type the argument here and
        say nothing to the generator. The cdef assignment below does
        the conversion, and its runtime check is the one the signature
        would have done."""
        cdef StorePath sp = path
        cdef CStore* store = self._get()
        cdef CStorePath* p = sp._get()
        cdef string out
        with nogil:
            out = real_path(deref(store), deref(p))
        return pathlib.Path(out.decode('utf-8'))

    def query_path_info(self, path: StorePath) -> PathInfo:
        """What this store knows about one path it holds.

        Raises InvalidPath when it does not hold it, which is a
        different answer from a malformed name: BadStorePath means the
        string is not a store path at all.

        The result is a VALUE - what the store said when asked - so it
        crosses the wire as a copy and a caller reads it without
        another round trip."""
        cdef StorePath sp = path
        cdef CStore* store = self._get()
        cdef CStorePath* p = sp._get()
        cdef CPathInfo out
        with nogil:
            out = path_info(deref(store), deref(p))
        cdef object deriver = None
        if not out.deriver.empty():
            deriver = StorePath(out.deriver.decode('utf-8'))
        return PathInfo._from_parts(
            StorePath(out.path.decode('utf-8')),
            out.nar_hash.decode('utf-8'),
            out.nar_size,
            deriver,
            out.registration_time,
            out.ultimate)

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
