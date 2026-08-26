"""
The real nix::Store (tasks/015).

Abstract in C++, and its implementation is chosen by a URI, so the
binding is constructed through openStore rather than a constructor.
"dummy://" is an in-memory store and needs nothing on disk, which is
what makes this testable in a build sandbox.
"""

# Declarations this one names. A declaration names another
# declaration's type by importing it, and the reader follows the
# import - nothing here runs, so this costs a parse.
from cythonix_idl.decl.content_address import ContentAddressMethod, HashAlgorithm
from cythonix_idl.decl.path import StorePath
from cythonix_idl.declare import (
    I64,
    U64,
    Bint,
    Bytes,
    Path,
    Str,
    binding,
    binds,
    blocks,
    cxx_body,
    cxx_name,
    cxx_parts,
    header,
    instant,
    needs,
    produced,
    startup,
    translator,
    wire_value,
)


@produced(by="Store.query_path_info")
@binding(threading="pool", blocking=False)
@wire_value()
class PathInfo:
    """What a store knows about one path it holds.

    A VALUE, not a handle: it is what the store said at the moment it
    was asked, so it crosses the wire as a copy and nothing about it
    can go stale in a way a caller could act on.

    Produced, never constructed. Every field comes from the store's
    own database, so there is nothing a caller could correctly build
    one from - which `_produced` says out loud rather than leaving to
    an inference elsewhere."""

    def path(self) -> "StorePath":
        """The path this describes."""

    def nar_hash(self) -> str:
        """The hash of the path's NAR serialisation, algorithm first:
        `sha256:<base32>`, the same spelling `nix path-info` prints."""

    def nar_size(self) -> U64:
        """The size of that NAR in bytes. Not the size on disk."""

    def deriver(self) -> "StorePath | None":
        """The .drv that built this, or None.

        None is a real answer, not a gap: a path added straight to the
        store was not built by anything."""

    def registration_time(self) -> I64:
        """When the store learnt about this path, as a Unix time."""

    def ultimate(self) -> bool:
        """Whether this store built it itself, as opposed to receiving
        it from a substituter or an import."""

    def ca(self) -> "str | None":
        """How this path's content addresses itself, or None.

        `fixed:r:sha256:<hash>` for a path added to the store, which
        is the same spelling `nix path-info --json` prints. None for a
        path that was BUILT: an input-addressed output is named after
        the derivation that made it, not after its own bytes, so
        there is nothing to address by.

        None rather than "": the two are different answers, and this
        is the first optional SCALAR field the wire can carry them
        both across (tasks/048)."""

    def references(self) -> "list[StorePath]":
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field. Nix scans the
        bytes for them at add time, so a path added from a directory
        of plain text has none.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""

    def sigs(self) -> "list[str]":
        """Who vouched for this path, as `<key-name>:<base64>`.

        Empty for a path this store added itself: a signature says a
        path came from somewhere and arrived intact, and a local add
        travelled nowhere."""


@produced(by="Store.to_store_path")
@binding(threading="pool", blocking=False)
@wire_value()
class StoreLocation:
    """Where one file sits: which store path holds it, and where
    inside.

    What `Store.to_store_path` answers. A store path names an OBJECT,
    and a file inside that object is not one - so the answer is a pair
    and both halves are needed to reach the file again.

    A VALUE, like PathInfo, and produced rather than constructed: it
    is the result of a split that only a store can perform, because
    only a store knows its own directory."""

    def path(self) -> "StorePath":
        """The store path that holds the file."""

    def sub_path(self) -> str:
        """Where the file sits inside it, leading slash included:
        `/bin/python3`.

        Empty when the path given WAS the store path. That is a real
        answer rather than a gap - there is nothing below it."""


@produced(by="open_store")
@header("nix/store/store-api.hh")
@binding(
    cxx="nix::Store",
    # A store carries its own locking, so any pool thread will do.
    threading="pool",
    # It talks to a daemon or a database. Every call can wait.
    blocking=True,
)
class Store:
    """A real nix::Store, opened from a URI.

    `Store("dummy://")` is in-memory. `Store("auto")` is whatever the
    ambient configuration says, which usually means the daemon."""

    def __init__(self, uri: Str) -> None:
        """Open a store from a URI.

        Not a C++ constructor. nix::Store is abstract and its
        implementation is chosen by the URI, so `@produced(by=...)`
        above names the factory that makes one - which is the same
        fact the binding carries as `_ctor_from`.

        The PARAMETERS are the constructor's, though, and they are
        what a caller sees: `Store(uri)` is the Python surface either
        way, so the declaration states it here rather than leaving the
        signature to be reflected off a compiled class."""

    # Reads a string the config already holds. Releasing the GIL
    # around it would cost two thread-state transitions to save
    # nothing.
    @instant
    @cxx_body("return s.config.getHumanReadableURI();")
    def get_uri(self) -> Str:
        """How this store describes itself.

        For logging only, upstream is explicit about that: it does
        not round-trip as a store reference and it is not a cache
        key.

        There is no getUri() any more. 2.34 moved it onto the config
        as getHumanReadableURI, and Store reaches its config by
        reference - which a pxd cannot describe without declaring the
        whole config type for the sake of one string."""
    @cxx_name("isValidPath")
    def is_valid_path(self, path: "StorePath") -> Bint:
        """Whether the store has that path."""
    # The first declared methods that take a VOCABULARY. A member IS
    # the string libstore parses, so it crosses as one and nothing
    # here translates - which is why the words are declared rather
    # than bound.
    @needs("nix/util/serialise.hh")
    @cxx_body("""std::string contents(data.c_str(), data.size());
        // An lvalue, so the string_view inside cannot dangle - which is
        // the case StringSource deletes its rvalue constructor to stop.
        nix::StringSource dump{contents};
        return s.addToStoreFromDump(
            dump,
            name,
            nix::FileSerialisationMethod::Flat,
            nix::ContentAddressMethod::parse(method),
            nix::parseHashAlgo(hash_algo),
            as_set<nix::StorePathSet>(references));""")
    def add_to_store(
        self,
        name: Str,
        data: Bytes,
        method: ContentAddressMethod = ContentAddressMethod.NAR,
        hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
        # The implicit Optional is the .pyx surface exactly, and the
        # generated protocol above already spells it
        # `list[StorePath] | None`. Writing the wider type here would
        # change what reflection reads back and say nothing new to a
        # caller, so the rule is silenced rather than followed.
        references: "list[StorePath]" = None,  # noqa: RUF013
    ) -> "StorePath":
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

        `references` is what the added path POINTS AT. Nix is told
        them; it does not scan an added path for them, so a path that
        mentions another and does not declare it is a broken closure
        the store will happily hold. None and an empty list mean the
        same thing, which is what lets the wire carry absence as a
        repeated field with nothing in it."""
    @needs("nix/util/posix-source-accessor.hh")
    @cxx_body("""auto source = nix::PosixSourceAccessor::createAtRoot(
            std::filesystem::weakly_canonical(std::filesystem::path{path}));
        return s.addToStore(
            name,
            source,
            nix::ContentAddressMethod::parse(method),
            nix::parseHashAlgo(hash_algo),
            as_set<nix::StorePathSet>(references));""")
    def add_path_to_store(
        self,
        name: Str,
        path: Str,
        method: ContentAddressMethod = ContentAddressMethod.NAR,
        hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
        # The implicit Optional is the .pyx surface exactly, and the
        # generated protocol above already spells it
        # `list[StorePath] | None`. Writing the wider type here would
        # change what reflection reads back and say nothing new to a
        # caller, so the rule is silenced rather than followed.
        references: "list[StorePath]" = None,  # noqa: RUF013
    ) -> "StorePath":
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
        repeated field with nothing in it."""
    @cxx_body("""return as_list(s.queryAllValidPaths());""")
    def query_all_valid_paths(self) -> "list[StorePath]":
        """Every path this store holds.

        Not every store answers it. nix::Store's own implementation
        raises "not supported by store", and only the local and remote
        stores override it - which is honest, because a substituter has
        no such list to give.

        Sorted, because libstore answers with a set."""
    @cxx_body("""return as_list(s.queryValidDerivers(path));""")
    def query_valid_derivers(
        self,
        path: "StorePath",
    ) -> "list[StorePath]":
        """Every derivation this store still holds that has `path` as
        an output.

        A different question from `PathInfo.deriver`, which names the
        .drv that actually BUILT this path - and which may be gone. A
        path that several derivations can produce has several derivers,
        and one whose .drv was collected has none.

        Empty is a normal answer. nix::Store's own implementation
        returns an empty set rather than raising, so a store that does
        not track this says nothing rather than failing."""
    @cxx_body("""return as_list(
            s.queryValidPaths(as_set<nix::StorePathSet>(paths)));""")
    def query_valid_paths(
        self,
        paths: "list[StorePath]",
    ) -> "list[StorePath]":
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
    # The first declared method with DEFAULTS. They are libstore's
    # own, not a judgement made here: a caller who omits all three
    # gets what `nix-store -qR` does.
    @cxx_body("""nix::StorePathSet out;
        s.computeFSClosure(
            as_set<nix::StorePathSet>(paths), out, flip_direction,
            include_outputs, include_derivers);
        return as_list(out);""")
    def compute_fs_closure(
        self,
        paths: "list[StorePath]",
        flip_direction: Bint = False,
        include_outputs: Bint = False,
        include_derivers: Bint = False,
    ) -> "list[StorePath]":
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
    @cxx_body("""nix::StorePathSet referrers;
        s.queryReferrers(path, referrers);
        return as_list(referrers);""")
    def query_referrers(
        self,
        path: "StorePath",
    ) -> "list[StorePath]":
        """Which store paths point AT this one.

        The inverse of `PathInfo.references`, and the direction a
        garbage collector reads: a path with referrers is one something
        else still needs.

        Only a store with a database can answer. nix::Store's own
        implementation raises "not supported by store", the way
        `query_all_valid_paths` does, because a substituter has no such
        index."""
    # A pathlib.Path, not a str, and the alias says so. The boundary
    # still carries a std::string; what changes above it is that this
    # answer names a file on THIS machine, so it is a path a caller
    # can open.
    @needs("nix/store/local-fs-store.hh")
    @cxx_body("""auto * fs = dynamic_cast<nix::LocalFSStore *>(&s);
        if (fs == nullptr)
            throw nix::Unsupported(
                "operation 'real_path' is not supported by store '%s'",
                s.config.getHumanReadableURI());
        return fs->toRealPath(path);""")
    def real_path(self, path: "StorePath") -> Path:
        """Where this store object's files really are.

        A different question from `print_store_path`, which joins the
        store DIRECTORY onto the path. A chroot store keeps
        /nix/store as its store directory and puts the files under
        <root>/nix/store, so its printed path does not exist and this
        one does.

        Only a store with a filesystem can answer. A remote or a
        binary-cache store raises "not supported by store", which is
        libstore's own refusal rather than one invented here.

        The answer is for the machine the store runs on. In process
        that is here; over RPC it is the server's (tasks/040)."""
    # The record crossing. Every member of the C++ struct, its
    # constructor, and every accessor Python reads come from
    # PathInfo's own declaration - so what is left here is the one
    # thing nothing can derive: where libstore keeps each part.
    @needs("nix/store/path-info.hh")
    @cxx_parts(
        "auto info = s.queryPathInfo(path);",
        path="info->path",
        # Nix32 with the algorithm in front, because that is what
        # `nix path-info` and a .narinfo print. The algorithm travels
        # with the digest, so a caller is never told separately which
        # one it is.
        nar_hash="info->narHash.to_string(nix::HashFormat::Nix32,"
                 " /*includeAlgo=*/true)",
        nar_size="info->narSize",
        # Straight across. libstore already keeps this as
        # std::optional<nix::StorePath>, and the record's member has
        # the same type - so absence stays absence rather than
        # becoming a sentinel a reader has to know about.
        deriver="info->deriver",
        registration_time="static_cast<std::int64_t>("
                          "info->registrationTime)",
        ultimate="info->ultimate",
        # Rendered, because there is no caster for nix::ContentAddress
        # and `fixed:r:sha256:<hash>` is the spelling `nix path-info
        # --json` prints.
        ca="info->ca ? std::optional<std::string>(info->ca->render())"
           " : std::nullopt",
        references="as_list(info->references)",
        # A set of nix::Signature, not of strings. Each one prints
        # itself, which is what a caller wants to see.
        sigs="to_strings(info->sigs)",
    )
    def query_path_info(self, path: "StorePath") -> "PathInfo":
        """What this store knows about one path it holds.

        Raises InvalidPath when it does not hold it, which is a
        different answer from a malformed name: BadStorePath means the
        string is not a store path at all.

        The result is a VALUE - what the store said when asked - so it
        crosses the wire as a copy and a caller reads it without
        another round trip."""
    @cxx_parts(
        "auto [store_path, sub] = s.config.toStorePath(path);",
        path="store_path",
        sub_path="sub.absOrEmpty()",
    )
    def to_store_path(self, path: Str) -> "StoreLocation":
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
    @cxx_name("queryPathFromHashPart")
    def query_path_from_hash_part(self, hash_part: Str) -> "StorePath | None":
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
    @cxx_body("return s.followLinksToStore(path).string();")
    def follow_links_to_store(self, path: Str) -> Str:
        """Follow symlinks until the path lands in the store, and
        stop there.

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
    @cxx_body("return s.followLinksToStorePath(path);")
    def follow_links_to_store_path(self, path: Str) -> "StorePath":
        """Follow symlinks until the path lands in the store, and say
        which store path it landed in.

        The whole of `follow_links_to_store`, minus the part below the
        store path. A `result` symlink pointing at a package resolves
        to `<store path>/bin/foo`, and this answers with the store
        path alone.

        The symlinks are read on the machine the store runs on. In
        process that is here; over RPC it is the server's filesystem.

        A path that is already in the store is answered without
        touching the filesystem at all: the loop tests that first. So
        this is a superset of `to_store_path`, minus the sub-path.

        Raises BadStorePath when the links run out somewhere else -
        the narrow type, unlike `to_store_path`, and that asymmetry is
        upstream's."""
    @cxx_name("printStorePath")
    def print_store_path(self, path: "StorePath") -> Str:
        """This path as the store spells it: its directory, then the
        base name.

        The store's directory, not this machine's. A chroot store
        keeps `/nix/store` in its paths while its files live under a
        root somewhere else, so this is what the store calls the path
        and `real_path` is where the bytes are."""
    # A bound object crosses back as an OWNING pointer, and the
    # emitter derives that from the return type alone: the signature,
    # the temporary, the NULL guard and the __new__-without-__init__
    # that takes ownership. What the body carries is the call and the
    # one decision C++ has to make about it.
    @cxx_body("return s.parseStorePath(path);")
    def parse_store_path(self, path: Str) -> "StorePath":
        """This string as a store path of THIS store.

        A store path is a base name, and which directory it belongs
        under is the store's fact rather than the name's. So parsing
        one is a question for a store: `/nix/store/<hash>-name` is a
        path of the default store and not of a chroot store rooted
        somewhere else.

        Raises when it is not in this store's directory - which is a
        different question from whether the name is well formed, and
        the reason this lives on the store rather than on StorePath."""


# A FREE binding: it belongs to no class, because it is what makes a
# class. `nix::openStore` picks an implementation from a URI, so
# there is no constructor to declare and `@produced(by="open_store")`
# on Store above names this function as the way in.
@needs("cythonix_bindings/_cpp/libstore.hpp")
@binds("cythonix::open_store")
@blocks
def open_store(uri: Str = "auto") -> "Store":
    """Open the store this URI names.

    The URI picks the implementation. "auto" is what the `nix` command
    itself uses: the daemon when one is running, the local store when
    it is not. "dummy://" is in memory and touches no disk, which is
    what makes a store testable in a build sandbox. A path opens a
    chroot store rooted there.

    The store stays open for as long as Python holds it. libstore
    hands back a reference-counted handle and the binding keeps it, so
    a store outliving the call that opened it is the normal case
    rather than a leak.

    Blocks. Opening a daemon store connects to it, and opening a local
    store may create its database.
    """


# --- what the module does before a caller exists -------------------

# Neither of these is surface. They are declared because this is
# where a module's C++ facts live, and a module that does not bind
# libstore needs neither - which is what the emitter used to assume
# and get wrong.


@needs("cythonix_bindings/_cpp/libstore.hpp")
@binds("cythonix::init_libstore")
@startup
def _init_libstore() -> None:
    """Initialise libstore, once, at import.

    libstore does not raise when it has not been initialised: it
    ABORTS the process, with "The program must call nix::initNix()
    before calling any libstore library functions". A binding cannot
    let a caller discover that, so this runs before anything else in
    the module - including the imports, which run another module's
    initialisation.
    """


@needs("cythonix_bindings/_cpp/errors.hpp")
@binds("cythonix::translate_nix_error")
@translator
def _translate_nix_error() -> None:
    """Map a nix exception onto the right class in errors.py.

    nix has an exception hierarchy worth keeping - BadStorePath is a
    different answer from InvalidPath - and nanobind's default would
    flatten every one of them to RuntimeError.
    """
