# cython: language_level=3
# cython: annotation_typing=False
# The real nix::Store (tasks/015).
#
# Abstract in C++, and its implementation is chosen by a URI, so the
# binding is constructed through openStore rather than a constructor.
# "dummy://" is an in-memory store and needs nothing on disk, which is
# what makes this testable in a build sandbox.

import pathlib

from cythonix_bindings import _value

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
    compute_fs_closure,
    follow_links_to_store,
    follow_links_to_store_path,
    init_libstore,
    open_store,
    parse_store_path,
    path_info,
    query_all_valid_paths,
    query_path_from_hash_part,
    query_referrers,
    query_valid_derivers,
    query_valid_paths,
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


cdef list _store_paths(const vector[string] & names):
    """A vector of base names as a list of StorePath.

    The inverse of `_base_names`, and the one place a set of store
    paths comes back into Python. Every set-returning call answers with
    base names for the reason `_cpp/store.hpp` gives, so every one of
    them lands here rather than growing its own loop."""
    cdef list out = []
    cdef size_t i
    for i in range(names.size()):
        out.append(StorePath(names[i].decode('utf-8')))
    return out


# PathInfo and StoreLocation are GENERATED, from
# spike-idl/decl/store.py. They are plain values - the store built
# them, so they hold Python slots and no C++ - and the emitter
# writes those in full. Store below is not: nix::Store is abstract
# and opened by a URI, so it stays hand-written beside them.
#
# An include, not an import: `include` splices the text into THIS
# module, so both classes keep __module__ == "cythonix_bindings.store"
# and every caller that already names them keeps working.
include "store_produced.pxi"


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

        Sorted, because libstore answers with a set."""
        cdef CStore* store = self._get()
        cdef vector[string] found
        with nogil:
            found = query_all_valid_paths(deref(store))
        return _store_paths(found)

    def query_valid_derivers(self, path: StorePath) -> list[StorePath]:
        """Every derivation this store still holds that has `path` as
        an output.

        A different question from `PathInfo.deriver`, which names the
        .drv that actually BUILT this path - and which may be gone. A
        path that several derivations can produce has several derivers,
        and one whose .drv was collected has none.

        Empty is a normal answer. nix::Store's own implementation
        returns an empty set rather than raising, so a store that does
        not track this says nothing rather than failing."""
        cdef StorePath sp = path
        cdef CStore* store = self._get()
        cdef CStorePath* p = sp._get()
        cdef vector[string] found
        with nogil:
            found = query_valid_derivers(deref(store), deref(p))
        return _store_paths(found)

    def query_valid_paths(self, paths: list[StorePath]) -> list[StorePath]:
        """Which of these paths this store actually holds.

        The set form of `is_valid_path`, and not merely a loop over
        it: a store that talks to a daemon answers the whole set in
        one round trip.

        Nothing is substituted. libstore's overload takes a flag that
        would go and FETCH what is missing, which is a different
        operation with a different cost - it belongs in its own
        binding rather than in a boolean here.

        Sorted, and shorter than what went in when the store is
        missing something."""
        cdef vector[string] c_paths = _base_names(paths)
        cdef CStore* store = self._get()
        cdef vector[string] found
        with nogil:
            found = query_valid_paths(deref(store), c_paths)
        return _store_paths(found)

    def compute_fs_closure(self, paths: list[StorePath],
                           flip_direction: bool = False,
                           include_outputs: bool = False,
                           include_derivers: bool = False
                           ) -> list[StorePath]:
        """Every path reachable from these, transitively.

        What `nix-store --query --requisites` answers, and the reason
        `references` is worth having: one edge is a fact, the closure
        is what a caller can copy, sign or delete as a unit. The
        starting paths are included.

        `flip_direction` walks referrers instead, so the answer is
        what would BREAK if these paths went away - the question a
        garbage collector asks.

        `include_outputs` and `include_derivers` widen the walk at a
        .drv: the first follows a derivation to what it builds, the
        second follows a path back to what could build it. Both are
        off, which is what `nix-store -qR` does.

        Sorted, because libstore answers with a set - so the order is
        NOT topological. A caller who needs build order has to ask for
        it another way."""
        cdef vector[string] c_paths = _base_names(paths)
        cdef bint c_flip = flip_direction
        cdef bint c_outputs = include_outputs
        cdef bint c_derivers = include_derivers
        cdef CStore* store = self._get()
        cdef vector[string] found
        with nogil:
            found = compute_fs_closure(
                deref(store), c_paths, c_flip, c_outputs, c_derivers)
        return _store_paths(found)

    def query_referrers(self, path: StorePath) -> list[StorePath]:
        """Which store paths point AT this one.

        The inverse of `PathInfo.references`, and the direction a
        garbage collector reads: a path with referrers is one something
        else still needs.

        Only a store with a database can answer. nix::Store's own
        implementation raises "not supported by store", the way
        `query_all_valid_paths` does, because a substituter has no such
        index."""
        cdef StorePath sp = path
        cdef CStore* store = self._get()
        cdef CStorePath* p = sp._get()
        cdef vector[string] found
        with nogil:
            found = query_referrers(deref(store), deref(p))
        return _store_paths(found)

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
        cdef object ca = None
        if not out.ca.empty():
            ca = out.ca.decode('utf-8')
        cdef list sigs = []
        for sig in out.sigs:
            sigs.append(sig.decode('utf-8'))
        return PathInfo._from_parts(
            StorePath(out.path.decode('utf-8')),
            out.nar_hash.decode('utf-8'),
            out.nar_size,
            deriver,
            out.registration_time,
            out.ultimate,
            ca,
            _store_paths(out.references),
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

    def query_path_from_hash_part(self, hash_part: str) -> StorePath | None:
        """Which store path has this hash part, or None.

        A store path's name begins with a 32-character base-32 hash,
        and that hash alone identifies the object: it is what a
        substituter is asked for, and what a `.narinfo` is named
        after. So this is the lookup that turns a bare hash back into
        a name.

        None is a normal answer, not a failure - the store does not
        have it. That is upstream's std::optional, and it is a
        different answer from `query_path_info`, which raises
        InvalidPath: there the caller named a path and was wrong,
        here the caller asked whether one exists."""
        cdef string c_hash = hash_part.encode('utf-8')
        cdef CStore* store = self._get()
        cdef CStorePath* out
        with nogil:
            out = query_path_from_hash_part(deref(store), c_hash)
        if out is NULL:
            return None
        cdef StorePath sp = StorePath.__new__(StorePath)
        sp._ptr = out
        return sp

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
