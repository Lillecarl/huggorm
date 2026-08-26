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
    CPathInfo,
    CStore,
    CStoreLocation,
    add_path_to_store,
    add_to_store,
    follow_links_to_store,
    follow_links_to_store_path,
    init_libstore,
    open_store,
    parse_store_path,
    path_info,
    query_all_valid_paths,
    real_path,
    store_uri,
    to_store_path,
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


cdef class StoreLocation:
    """Where one file sits: which store path holds it, and where
    inside.

    What `Store.to_store_path` answers. A store path names an OBJECT,
    and a file inside that object is not one - so the answer is a pair
    and both halves are needed to reach the file again.

    A VALUE, like PathInfo, and produced rather than constructed: it
    is the result of a split that only a store can perform, because
    only a store knows its own directory."""

    _threading = "pool"
    # Two reads of memory this object already owns.
    _blocking = False
    _wire = "value"
    _produced = True
    # `sub_path` is empty when the path IS the store path. Empty, not
    # absent: there is a real answer and it is "nothing below it", so
    # the field carries no "?".
    _wire_fields = (
        ("path", "StorePath"),
        ("sub_path", "str"),
    )

    cdef object _path
    cdef object _sub_path

    def __init__(self):
        raise TypeError(
            "StoreLocation objects come from Store.to_store_path, not from a "
            "constructor")

    def path(self) -> StorePath:
        """The store path that holds the file."""
        return self._path

    def sub_path(self) -> str:
        """Where the file sits inside it, leading slash included:
        `/bin/python3`.

        Empty when the path given WAS the store path. That is a real
        answer rather than a gap - there is nothing below it."""
        return self._sub_path

    @classmethod
    def _from_parts(cls, path, sub_path):
        """Wire-deserialization helper (private, never surfaced)."""
        cdef StoreLocation loc = StoreLocation.__new__(StoreLocation)
        loc._path = path
        loc._sub_path = sub_path
        return loc

    def _parts(self):
        """Wire-serialization helper (private): one value per
        _wire_fields entry, in order."""
        return (self._path, self._sub_path)


cdef vector[string] _base_names(object paths) except *:
    """A list of StorePath as the vector the shims take.

    None and an empty list are the same answer. That is not a
    convenience: a repeated protobuf field has no presence, so absence
    cannot travel as anything else - which is why a container is the
    one parameter type whose default may be None.

    Base names, not printed paths. A StorePath is a name and the store
    supplies the directory, so sending a printed one would pick a
    store directory that the caller has no business choosing."""
    cdef vector[string] out
    if paths is None:
        return out
    for path in paths:
        out.push_back((<StorePath?>path).to_string().encode('utf-8'))
    return out


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
    # Eight reads of memory this object already owns. Nothing blocks,
    # so there is no thread to hop to and the codegen emits no async
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
    #
    # `references` and `sigs` carry no `?` and cannot: a repeated
    # protobuf field has no presence, so an absent one IS an empty one
    # - which is also the right answer here, because a path with
    # nothing to point at has no references rather than unknown ones.
    _wire_fields = (
        ("path", "StorePath"),
        ("nar_hash", "str"),
        ("nar_size", "int"),
        ("deriver", "StorePath?"),
        ("registration_time", "int"),
        ("ultimate", "bool"),
        ("references", "list[StorePath]"),
        ("sigs", "list[str]"),
    )

    cdef object _path
    cdef object _nar_hash
    cdef object _nar_size
    cdef object _deriver
    cdef object _registration_time
    cdef object _ultimate
    cdef object _references
    cdef object _sigs

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

    def references(self) -> list[StorePath]:
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field. Nix scans the
        bytes for them at add time, so a path added from a directory
        of plain text has none.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""
        return self._references

    def sigs(self) -> list[str]:
        """Who vouched for this path, as `<key-name>:<base64>`.

        Empty for a path this store added itself: a signature says a
        path came from somewhere and arrived intact, and a local add
        travelled nowhere."""
        return self._sigs

    @classmethod
    def _from_parts(cls, path, nar_hash, nar_size, deriver,
                    registration_time, ultimate, references, sigs):
        """Wire-deserialization helper (private, never surfaced)."""
        cdef PathInfo info = PathInfo.__new__(PathInfo)
        info._path = path
        info._nar_hash = nar_hash
        info._nar_size = nar_size
        info._deriver = deriver
        info._registration_time = registration_time
        info._ultimate = ultimate
        info._references = references
        info._sigs = sigs
        return info

    def _parts(self):
        """Wire-serialization helper (private): one value per
        _wire_fields entry, in order."""
        return (self._path, self._nar_hash, self._nar_size, self._deriver,
                self._registration_time, self._ultimate, self._references,
                self._sigs)


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
                     hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
                     references: list[StorePath] = None
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
        reach the codegen as Any.

        `references` is what the added path POINTS AT. Nix is told
        them; it does not scan an added path for them, so a path that
        mentions another and does not declare it is a broken closure
        the store will happily hold. None and an empty list mean the
        same thing, which is what lets the wire carry absence as a
        repeated field with nothing in it.
        """
        cdef string c_name = name.encode('utf-8')
        cdef string c_data = data
        cdef string c_method = method.encode('utf-8')
        cdef string c_algo = hash_algo.encode('utf-8')
        cdef vector[string] c_refs = _base_names(references)
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = add_to_store(
                deref(store), c_name, c_data, c_method, c_algo, c_refs)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp

    def add_path_to_store(self, name: str, path: str,
                          method: ContentAddressMethod = ContentAddressMethod.NAR,
                          hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
                          references: list[StorePath] = None
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
        layer that knows what it was for.

        `references` is what the added path POINTS AT. Nix is told
        them; it does not scan an added path for them, so a path that
        mentions another and does not declare it is a broken closure
        the store will happily hold. None and an empty list mean the
        same thing, which is what lets the wire carry absence as a
        repeated field with nothing in it.
        """
        cdef string c_name = name.encode('utf-8')
        cdef string c_path = path.encode('utf-8')
        cdef string c_method = method.encode('utf-8')
        cdef string c_algo = hash_algo.encode('utf-8')
        cdef vector[string] c_refs = _base_names(references)
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = add_path_to_store(
                deref(store), c_name, c_path, c_method, c_algo, c_refs)
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
        cdef list references = []
        cdef list sigs = []
        for ref in out.references:
            references.append(StorePath(ref.decode('utf-8')))
        for sig in out.sigs:
            sigs.append(sig.decode('utf-8'))
        return PathInfo._from_parts(
            StorePath(out.path.decode('utf-8')),
            out.nar_hash.decode('utf-8'),
            out.nar_size,
            deriver,
            out.registration_time,
            out.ultimate,
            references,
            sigs)

    def to_store_path(self, path: str) -> StoreLocation:
        """Which store path CONTAINS this file, and where inside it.

        A different question from `parse_store_path`, which takes the
        store path itself and refuses anything below it. An
        interpreter lives at `<store path>/bin/python3`, which is a
        file in a store object and is not a store object - so asking
        which one holds it needs this call.

        String work only: it splits on the store DIRECTORY and never
        touches the filesystem. So it answers for a path that does not
        exist, and it answers in the store's own terms rather than in
        this machine's - which is why a chroot store answers about
        `/nix/store/...` and not about `<root>/nix/store/...`.
        `real_path` is the call that goes the other way.

        Symlinks are not followed. nix::Store has
        followLinksToStorePath for that, and it is a different call
        with a different failure mode: it reads the filesystem.

        A path outside the store raises NixError, not BadStorePath.
        That is upstream's answer: StoreDirConfig::toStorePath throws
        a bare Error where parseStorePath throws BadStorePath, for the
        same fact. The binding does not correct it - it would then
        disagree with `nix` for the same input - so a caller catching
        the narrow type around both calls must catch the wide one
        here."""
        cdef string c_path = path.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStoreLocation out
        with nogil:
            out = to_store_path(deref(store), c_path)
        return StoreLocation._from_parts(
            StorePath(out.path.decode('utf-8')),
            out.sub_path.decode('utf-8'))

    def follow_links_to_store(self, path: str) -> str:
        """Follow symlinks until the path lands in the store, and stop
        there.

        The first half of `follow_links_to_store_path`, and the half
        that keeps what the other one drops. A `result` symlink
        pointing at a package resolves to `<store path>/bin/foo`; the
        other call answers with the store path alone.

        A str, not a pathlib.Path, and the difference is real. The
        answer is in the STORE's terms - the same spelling
        `print_store_path` gives - so its directory is the store
        directory, which a chroot store keeps at `/nix/store` while
        its files live under `<root>/nix/store`. `real_path` is the
        call that answers where the bytes are on THIS machine, and it
        returns a path because it can.

        The symlinks are read on the machine the store runs on. In
        process that is here; over RPC it is the server's filesystem,
        which is what makes this a remote call worth having - and the
        same meaning `add_path_to_store` already carries.

        Raises BadStorePath when the links run out somewhere else."""
        cdef string c_path = path.encode('utf-8')
        cdef CStore* store = self._get()
        cdef string out
        with nogil:
            out = follow_links_to_store(deref(store), c_path)
        return out.decode('utf-8')

    def follow_links_to_store_path(self, path: str) -> StorePath:
        """The same question as `to_store_path`, asked of a symlink.

        `to_store_path` is string work and never reads the filesystem,
        so it cannot answer for `/run/current-system` or for a
        `result` symlink: neither is in the store, and both point at
        something that is. This one follows links until it lands in
        the store, then splits.

        Only the store path comes back. Upstream drops the sub-path
        here and this does too - the file the caller named is not
        where the link pointed, so a sub-path taken from the RESOLVED
        path would name something the caller never asked about.

        A relative path is resolved against the working directory, by
        libstore rather than here. Over RPC that is the SERVER's
        working directory, which is another reason to pass an absolute
        one.

        A path that is already in the store is answered without
        touching the filesystem at all: the loop tests that first. So
        this is a superset of `to_store_path`, minus the sub-path.

        Raises BadStorePath when the links run out somewhere else -
        the narrow type, unlike `to_store_path`, and that asymmetry is
        upstream's."""
        cdef string c_path = path.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = follow_links_to_store_path(deref(store), c_path)
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp

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
