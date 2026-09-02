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
from huggorm_decl.decl.build_result import KeyedBuildResult
from huggorm_decl.decl.derived_path import DerivedPath
from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.pathinfo import PathInfo
from huggorm_decl.decl.realisation import DrvOutput, Realisation
from huggorm_decl.decl.words import (
    BuildMode,
    ContentAddressMethod,
    HashAlgorithm,
    TrustedFlag,
)
from huggorm_dsl.declare import (
    U64,
    Bint,
    Bytes,
    Cxx,
    Path,
    Str,
    StrView,
    abstract,
    binding,
    binds,
    blocks,
    cxx_name,
    header,
    instant,
    needs,
    produced,
    reads,
    startup,
    translator,
    wire_value,
)


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

@produced(by="Store.query_missing")
@header("nix/store/store-api.hh")
@binding(
    cxx="nix::MissingPaths",
    # Five members the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value()
class MissingPaths:
    """What a build would have to do, without doing any of it.

    The answer to "what is missing", which is the question a caller
    asks before deciding whether to build at all: how much would be
    fetched, how much would be built here, and how much this store
    cannot account for.

    A VALUE, and the honest kind: it is a snapshot of what the store
    believed when it was asked, so nothing about it can go stale in a
    way a caller could act on without asking again.
    """

    @reads("willBuild")
    def will_build(self) -> "list[StorePath]":
        """The derivations that would be BUILT here.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""

    @reads("willSubstitute")
    def will_substitute(self) -> "list[StorePath]":
        """The outputs that would be FETCHED from a substituter."""

    @reads("unknown")
    def unknown(self) -> "list[StorePath]":
        """The paths this store cannot account for at all.

        Neither buildable nor substitutable from here - usually a
        derivation this store does not have."""

    @reads("downloadSize")
    def download_size(self) -> U64:
        """Bytes that would come over the network, compressed.

        What a substituter would send, not what lands on disk."""

    @reads("narSize")
    def nar_size(self) -> U64:
        """Bytes the substituted paths take once unpacked."""

    def _from_parts() -> "MissingPaths":
        """Rebuild one from the parts that crossed.

        An aggregate, so this is one brace - but the three path lists
        cross as LISTS and libstore keeps them in sets, which is the
        same conversion every set-valued accessor here makes in the
        other direction."""
        Cxx("""
return nix::MissingPaths{
    as_set<nix::StorePathSet>(will_build),
    as_set<nix::StorePathSet>(will_substitute),
    as_set<nix::StorePathSet>(unknown),
    download_size,
    nar_size,
};
        """)

# The C++ FACT, and it could not be stated until 061 split it from
# "Python may not construct one". nix::Store has pure virtuals -
# every store this hands back is really a LocalStore or a
# UDSRemoteStore - and it is still opened by `Store(uri)`, because
# openStore answers the abstractness with a concrete subclass.
@abstract
@produced(by="open_store")
@header("nix/store/store-api.hh")
@binding(
    cxx="nix::Store",
    # openStore hands back a ref<Store>, which is a shared_ptr that
    # cannot be null. Python keeps a share, so a store stays open for
    # as long as the object naming it does.
    holder="shared_ptr",
    # A store carries its own locking, so any pool thread will do.
    threading="pool",
    # It talks to a daemon or a database. Every call can wait.
    blocking=True,
)
class Store:
    """A real nix::Store, opened from a URI.

    `Store("dummy://")` is in-memory. `Store("auto")` is whatever the
    ambient configuration says, which usually means the daemon."""

    # PROSE only, and the reader enforces that. `@produced(by=...)`
    # above names `open_store` as what builds one, so `open_store`
    # owns the signature - its parameters and its defaults are what a
    # caller passes to `Store(...)`. Declaring them here too is how
    # the `uri="auto"` default died: two statements of one signature,
    # and the emitter read the one without the default.
    #
    # Written anyway, because this is where a caller looks: they call
    # `Store(...)`, not `open_store(...)`, and the docstring belongs
    # under the name they type.
    def __init__(self) -> None:
        """Open a store from a URI.

        Not a C++ constructor. nix::Store is abstract and its
        implementation is chosen by the URI, so `@produced(by=...)`
        above names the factory that makes one - which is the same
        fact the binding carries as `_ctor_from`."""

    # Reads a string the config already holds. Releasing the GIL
    # around it would cost two thread-state transitions to save
    # nothing.
    @instant
    def get_uri(self) -> Str:
        """How this store describes itself.

        For logging only, upstream is explicit about that: it does
        not round-trip as a store reference and it is not a cache
        key.

        There is no getUri() any more. 2.34 moved it onto the config
        as getHumanReadableURI, and Store reaches its config by
        reference - so this reads through the member rather than
        declaring the whole config type for one string."""
        Cxx("return self.config.getHumanReadableURI();")
    @cxx_name("isValidPath")
    def is_valid_path(self, path: "StorePath") -> Bint:
        """Whether the store has that path."""
    # The first declared methods that take a VOCABULARY. A member IS
    # the string libstore parses, so it crosses as one and nothing
    # here translates - which is why the words are declared rather
    # than bound.
    @needs("nix/util/serialise.hh")
    def add_to_store(
        self,
        name: Str,
        data: Bytes,
        method: ContentAddressMethod = ContentAddressMethod.NAR,
        hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
        # The implicit Optional is the SURFACE exactly, and the
        # generated protocol above already spells it
        # `list[StorePath] | None`. Writing the wider type here would
        # say nothing new to a caller, so the rule is silenced rather
        # than followed.
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
        Cxx("""
std::string contents(data.c_str(), data.size());
// An lvalue, so the string_view inside cannot dangle - which is
// the case StringSource deletes its rvalue constructor to stop.
nix::StringSource dump{contents};
return self.addToStoreFromDump(
    dump,
    name,
    nix::FileSerialisationMethod::Flat,
    method,
    hash_algo,
    as_set<nix::StorePathSet>(references));
        """)
    @needs("nix/util/posix-source-accessor.hh")
    def add_path_to_store(
        self,
        name: Str,
        path: Str,
        method: ContentAddressMethod = ContentAddressMethod.NAR,
        hash_algo: HashAlgorithm = HashAlgorithm.SHA256,
        # The implicit Optional is the SURFACE exactly, and the
        # generated protocol above already spells it
        # `list[StorePath] | None`. Writing the wider type here would
        # say nothing new to a caller, so the rule is silenced rather
        # than followed.
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
        Cxx("""
auto source = nix::PosixSourceAccessor::createAtRoot(
    std::filesystem::weakly_canonical(std::filesystem::path{path}));
return self.addToStore(
    name,
    source,
    method,
    hash_algo,
    as_set<nix::StorePathSet>(references));
        """)
    @cxx_name("queryAllValidPaths")
    def query_all_valid_paths(self) -> "list[StorePath]":
        """Every path this store holds.

        Not every store answers it. nix::Store's own implementation
        raises "not supported by store", and only the local and remote
        stores override it - which is honest, because a substituter has
        no such list to give.

        Sorted, because libstore answers with a set."""

    @cxx_name("queryValidDerivers")
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

    @cxx_name("queryValidPaths")
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
        Cxx("""
nix::StorePathSet out;
self.computeFSClosure(
    as_set<nix::StorePathSet>(paths), out, flip_direction,
    include_outputs, include_derivers);
return as_list(out);
        """)
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
        Cxx("""
nix::StorePathSet referrers;
self.queryReferrers(path, referrers);
return as_list(referrers);
        """)
    # A pathlib.Path, not a str, and the alias says so. The boundary
    # still carries a std::string; what changes above it is that this
    # answer names a file on THIS machine, so it is a path a caller
    # can open.
    @needs("nix/store/local-fs-store.hh")
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
        Cxx("""
auto * fs = dynamic_cast<nix::LocalFSStore *>(&self);
if (fs == nullptr)
    throw nix::Unsupported(
        "operation 'real_path' is not supported by store '%s'",
        self.config.getHumanReadableURI());
return fs->toRealPath(path);
        """)
    # Nothing about PathInfo is here any more, and that is the whole
    # of tasks/056. It binds nix::ValidPathInfo, in `decl/pathinfo.py`
    # beside the header it comes from, and this file IMPORTS it. What
    # is left is the call.
    #
    # `queryPathInfo` answers a `ref<const ValidPathInfo>` - a share
    # of the store's own cached entry - and this dereferences into a
    # copy. Holding the share instead is available and is not needed
    # yet: a VALUE is what the store said when it was asked, and a
    # caller who holds one should not see it change.
    def query_path_info(self, path: "StorePath") -> "PathInfo":
        """What this store knows about one path it holds.

        Raises InvalidPath when it does not hold it, which is a
        different answer from a malformed name: BadStorePath means the
        string is not a store path at all.

        The result is a VALUE - what the store said when asked - so it
        crosses the wire as a copy and a caller reads it without
        another round trip."""
        Cxx("return *self.queryPathInfo(path);")
    # Pure string work: no daemon, no lock, no file. Releasing
    # the GIL around it costs two thread-state transitions to
    # save nothing, and these are the calls a caller makes most.
    @instant
    # An ordinary body. It used to be `@cxx_parts`: a prelude plus one
    # C++ expression per declared field, which described StoreLocation
    # a second time, on the method that returns one.
    #
    # StoreLocation's own accessors already say what its fields are -
    # that is where `record_fields` reads them - so all this needs to
    # say is how to build one. The emitter still declares the struct,
    # because `toStorePath` answers a std::pair and there is no
    # upstream type to bind.
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
        Cxx("""
auto [store_path, sub] = self.config.toStorePath(path);
return huggorm::StoreLocation{store_path, sub.absOrEmpty()};
        """)
    # UPSTREAM answers an `UnkeyedRealisation` here, because the key
    # is the one you passed. `decl/realisation.py` says why the
    # binding hands back the KEYED type instead, and this is where
    # the key is put back: it is the caller's own argument.
    @needs("nix/store/realisation.hh")
    def query_realisation(self, id: "DrvOutput") -> "Realisation | None":
        """What a CA derivation's output turned out to be, or None.

        None is a normal answer twice over. The store may simply not
        have realised that output - and a store with `ca-derivations`
        turned off answers None for EVERY id, because there is no
        mapping to consult. libstore decides that, not this binding:
        `LocalStore::queryRealisationUncached` checks the experimental
        feature and hands back nothing.

        So a caller who gets None has learnt that this store cannot
        tell them, which is different from learning the output does
        not exist. Nothing here can tell the two apart, and pretending
        otherwise would need a second question."""
        Cxx("""
auto found = self.queryRealisation(id);
if (!found)
    return std::nullopt;
return nix::Realisation{*found, id};
        """)
    # The first method taking a UNION, and the reason the union
    # mechanism exists (tasks/059). A target is a path to fetch or
    # outputs to build, and upstream says so with a std::variant.
    #
    # READ-ONLY, which is why this is the first consumer rather than
    # `build_paths`: it answers against a chroot store with nothing
    # built, so the hermetic suite can exercise the whole shape.
    def query_missing(self, targets: "list[DerivedPath]") -> "MissingPaths":
        """What building these would have to do.

        Nothing is built, fetched or locked. A path already valid here
        appears in none of the three lists - it is not missing - so an
        empty answer means there is nothing to do.

        `unknown` is the interesting one: a derivation this store does
        not hold cannot be planned around, and saying so is different
        from saying it needs building."""
        Cxx("return self.queryMissing(targets);")
    # The first declared MAP over a bound class, and the method that
    # was waiting for it. `OutputPathMap` is a
    # `std::map<std::string, StorePath>` upstream, and until the
    # emitter could spell one the only dict the DSL had was a
    # hard-coded `dict[str, int]` serving one free function.
    @cxx_name("queryDerivationOutputMap")
    def query_derivation_output_map(
            self, path: "StorePath") -> "dict[str, StorePath]":
        """Which path each of this derivation's outputs has.

        Keyed by output NAME - `out`, `dev`, `man` - because that is
        how a derivation names them and how a caller asks. The order
        is the map's, which is upstream's, which is alphabetical.

        Assumes every output has a path and raises otherwise, and
        that is upstream's own contract rather than a choice here:
        `queryStaticPartialDerivationOutputMap` is the one that
        answers with holes in it, and it is a different method.

        A path that is NOT a derivation answers TWO ways, measured:
        an empty dict inside a build sandbox, and libstore's "is not
        a valid derivation path" outside one. The difference is where
        `readInvalidDerivation` reaches its name check, not anything
        this binding does - so a caller gets one or the other and
        should not depend on which.

        No `evalStore`, the same second parameter `build_paths`
        leaves out and for the same reason. A body rather than a
        method pointer only because of that: upstream takes two
        arguments and this binds one, so there is a call to write."""
        Cxx("return self.queryDerivationOutputMap(path);")

    # `query_missing` says what building these WOULD do; this does it.
    # The pair is upstream's own, and it is why the union exists
    # (tasks/059): both take the same list, and only one of them
    # changes the store.
    def build_paths(self, targets: "list[DerivedPath]",
                    mode: "BuildMode" = BuildMode.NORMAL) -> None:
        """Build or fetch every one of these, and wait.

        A target that is a derivation gets BUILT, which means its
        outputs are made valid - by substituting them if they can be
        substituted, and by running the builder if they cannot,
        recursively through the inputs. A target that is a plain
        store path gets SUBSTITUTED.

        Already valid is a no-op, and that is upstream's own word for
        it. So building twice costs nothing the second time, and
        building an empty list does nothing at all.

        Raises rather than reporting. A failed build is a
        `nix::Error`, so it reaches Python as a NixError with
        libstore's message. Upstream's `buildPathsWithResults` is the
        other shape - a result per target, no exception - and it needs
        a `BuildResult` value declared, which is its own task.

        `mode` says what the build is FOR. `normal` stops as soon as
        the outputs are valid; `repair` replaces one whose contents
        no longer hash to its name; `check` rebuilds a valid output
        and compares without replacing it. The default is upstream's.

        No `evalStore`. It is a second store the caller supplies for
        derivations only, and the shape a Python caller wants for
        that is a question rather than a parameter to pass through."""
        Cxx("self.buildPaths(targets, mode);")

    @needs("nix/store/build-result.hh")
    @cxx_name("buildPathsWithResults")
    def build_paths_with_results(
            self, targets: "list[DerivedPath]",
            mode: "BuildMode" = BuildMode.NORMAL,
    ) -> "list[KeyedBuildResult]":
        """Build or fetch every one of these, and report on each.

        The same work `build_paths` does, and the other shape of the
        answer. Upstream's own comment says the difference: this does
        not throw on a build error, it returns a result carrying the
        message. So a caller building twenty targets gets twenty
        results and finds out what happened to each, where
        `build_paths` raises on the first failure and says nothing
        about the rest.

        One result per target, in the order the targets were given.
        Each holds the target it is about, so a caller can match them
        up without relying on that order.

        It still raises for the things that are not a build failure -
        an invalid path, a store that cannot do this. The value shape
        is for a build that RAN and did not produce the outputs, and
        not for a caller who asked something incoherent.

        No `evalStore`, the same third parameter `build_paths` leaves
        out and for the same reason."""
        Cxx("return self.buildPathsWithResults(targets, mode);")
    @cxx_name("ensurePath")
    def ensure_path(self, path: "StorePath") -> None:
        """Make this path valid, by substituting it if it is not.

        The narrow half of `build_paths`. That one takes a target
        which may be a derivation and will RUN a builder; this takes
        a path and will only fetch. So a caller who wants a binary
        cache hit and no local build asks for this, and finds out by
        the raise rather than by waiting.

        Already valid is a no-op. Nothing to substitute from raises,
        in libstore's own words."""
    @cxx_name("addTempRoot")
    def add_temp_root(self, path: "StorePath") -> None:
        """Keep this path from the collector while the store is open.

        A temporary root, held by THIS process: `collect_garbage`
        here or in any other process will not take the path until the
        store object goes away. That is the answer to the race
        upstream documents - root the path BEFORE checking whether it
        is valid, because between the check and the use is where the
        collector runs.

        `add_to_store` already calls this for what it adds, so a
        caller who only adds needs nothing. A caller who BUILDS does:
        `build_paths` does not root its targets, and upstream is
        explicit that rooting them is the caller's job and would be
        too late if the call did it.

        A store with no garbage collector does NOTHING here, and does
        not say so - upstream logs a debug line and returns. So this
        is not a promise the path is rooted; it is a promise this
        store was asked. A binary cache has nothing to root."""
    @cxx_name("querySubstitutablePaths")
    def query_substitutable_paths(
            self, paths: "list[StorePath]") -> "list[StorePath]":
        """Which of these this store could FETCH rather than build.

        Asked of the substituters, not of this store: a path already
        valid here is not the question. So the answer says what a
        `build_paths` would get cheaply, and the difference from
        `query_missing` is the grain - that one plans a whole target
        including its derivations, this one answers about paths.

        Shorter than what went in, and sorted, because upstream keeps
        them in a set."""
    @cxx_name("topoSortPaths")
    def topo_sort_paths(self, paths: "list[StorePath]") -> "list[StorePath]":
        """These paths in reference order: a path before what it needs.

        If p refers to q then p comes before q. That is the order to
        DELETE in, and the reverse of the order to register in - so a
        caller copying a closure into another store walks this
        backwards.

        `compute_fs_closure` answers WHICH paths; this answers in what
        order. The pair is how a closure becomes something a caller
        can act on one path at a time.

        Raises on a cycle. A store path graph cannot have one, so
        that is a corrupt store rather than a bad ask."""
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

    @cxx_name("isTrustedClient")
    def is_trusted_client(self) -> "TrustedFlag | None":
        """Whether this store trusts US, or None if it cannot say.

        Not a bool, because the answer has three values and upstream
        says so with `std::optional<TrustedFlag>`. A binary cache over
        HTTP returns nothing at all - it has no notion of who is
        asking - and that is a different answer from being untrusted.

        Whether the STORE trusts the client, which is upstream's own
        clarification and reads backwards at first. A store's
        `trusted` setting is the other question, whether we trust
        what comes out of it, and neither implies the other.

        Worth asking before a call that an untrusted client cannot
        make: repairing a path, or adding one with signatures. A
        local store trusts everyone, and a daemon decides from the
        connecting user."""
        Cxx("""
auto flag = self.isTrustedClient();
if (!flag)
    return std::nullopt;
return huggorm::as_word(*flag);
        """)

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
        Cxx("return self.followLinksToStore(path).string();")
    @cxx_name("followLinksToStorePath")
    def follow_links_to_store_path(self, path: StrView) -> "StorePath":
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
    # Pure string work: no daemon, no lock, no file. Releasing
    # the GIL around it costs two thread-state transitions to
    # save nothing, and these are the calls a caller makes most.
    @instant
    @cxx_name("printStorePath")
    def print_store_path(self, path: "StorePath") -> Str:
        """This path as the store spells it: its directory, then the
        base name.

        The store's directory, not this machine's. A chroot store
        keeps `/nix/store` in its paths while its files live under a
        root somewhere else, so this is what the store calls the path
        and `real_path` is where the bytes are."""
    # A bound object crosses back as itself, and the emitter derives
    # the whole binding from the return type alone. All this says is
    # what C++ calls the method.
    # Pure string work: no daemon, no lock, no file. Releasing
    # the GIL around it costs two thread-state transitions to
    # save nothing, and these are the calls a caller makes most.
    # The other half of 040's split, for the other path type. A
    # DerivedPath cannot print itself - `DerivedPath::to_string` takes
    # a StoreDirConfig by upstream's own signature - so rendering is a
    # store's act, exactly as it is for a StorePath.
    @instant
    def print_derived_path(self, target: "DerivedPath") -> Str:
        """This target as the store spells it.

        `<drv>^out,dev` for outputs of a derivation, `^*` for all of
        them, and a plain store path for the opaque arm. The `^`
        spelling, not the `!` one: upstream keeps both and `^` is what
        the command line takes."""
        Cxx("return target.to_string(self.config);")
    # Pure string work: no daemon, no lock, no file.
    @instant
    def parse_derived_path(self, target: StrView) -> "DerivedPath":
        """Read back what `print_derived_path` wrote.

        Raises when the string is not one, in libstore's own words.
        The nested arm is behind the `dynamic-derivations`
        experimental feature, so a `^` inside a `^` says so rather
        than being read as something else."""
        Cxx("return nix::DerivedPath::parse(self.config, target);")
    @instant
    @cxx_name("parseStorePath")
    def parse_store_path(self, path: StrView) -> "StorePath":
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
@needs("nix/store/store-open.hh")
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
    # `nix::openStore` returns a `ref<Store>` - a shared_ptr that
    # cannot be null - and `ref` defines an implicit conversion to
    # the `shared_ptr` nanobind holds as this class. So the body is
    # the call and nothing else has to say anything.
    #
    # This was three lines in `cpp/libstore.hpp` because a factory
    # had to be a NAMED C++ symbol. It does not any more, so the one
    # call lives beside the declaration that describes it
    # (tasks/063).
    Cxx("return nix::openStore(uri);")

# --- what the module does before a caller exists -------------------

# Neither of these is surface. They are declared because this is
# where a module's C++ facts live, and a module that does not bind
# libstore needs neither - which is what the emitter used to assume
# and get wrong.

@needs("huggorm_decl/cpp/libstore.hpp")
@binds("huggorm::init_libstore")
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

@needs("huggorm_decl/cpp/errors.hpp")
@binds("huggorm::translate_nix_error")
@translator
def _translate_nix_error() -> None:
    """Map a nix exception onto the right class in errors.py.

    nix has an exception hierarchy worth keeping - BadStorePath is a
    different answer from InvalidPath - and nanobind's default would
    flatten every one of them to RuntimeError.
    """
