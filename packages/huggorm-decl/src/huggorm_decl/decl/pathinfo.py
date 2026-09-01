"""
nix::ValidPathInfo, and everything about it, in one file.

One Nix class, one declaration, named after its header. `decl/store.py`
IMPORTS this to learn the shape rather than restating it, which is what
the old `@cxx_parts` field map did - nine keyword entries, in a second
vocabulary, on the method that happens to return one (tasks/056).

The C++ lives in bodies. The only strings here are C++.
"""

# The declaration this one names. A declaration names another
# declaration's type by importing it, and the reader follows the
# import.
from huggorm_decl.decl.content_address import ContentAddress
from huggorm_decl.decl.hash import Hash
from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.signature import Signature
from huggorm_dsl.declare import (
    I64,
    U64,
    Bint,
    Cxx,
    Str,
    binding,
    header,
    needs,
    produced,
    reads,
    wire_value,
)


@produced(by="Store.query_path_info")
@header("nix/store/path-info.hh")
@binding(
    cxx="nix::ValidPathInfo",
    # Every accessor reads memory the object already owns.
    threading="pool",
    blocking=False,
)
@wire_value()
class PathInfo:
    """What a store knows about one path it holds.

    A VALUE, not a handle: it is what the store said at the moment it
    was asked, so it crosses the wire as a copy and nothing about it
    can go stale in a way a caller could act on.

    Produced, never constructed. Every field comes from the store's
    own database, so there is nothing a caller could correctly build
    one from.
    """

    # WIRE ORDER. These accessors ARE the message, in this order, and
    # nothing lists them a second time.
    #
    # `@reads` is a data member the emitter binds whole. A `Cxx` body
    # is a decision a person made.
    #
    # There used to be five bodies here and every one was the same
    # decision: a RENDERING. libstore keeps a hash, a content address
    # and a set of signatures as its own types, and the wire carried
    # the text `nix path-info` prints - so a reader who wanted the
    # algorithm, or which key signed a path, parsed the string back.
    # Those three are their own messages now, and three of the bodies
    # went with them.

    # UPSTREAM's name and UPSTREAM's type. `ValidPathInfo::path` is a
    # StorePath member called `path`, so this is a member read and
    # nothing else. Rendering it against a store directory is a
    # separate act with an answer already: `store.print_store_path(p)`
    # (tasks/040, tasks/042).
    @reads("path")
    def path(self) -> "StorePath":
        """The path this describes."""

    @reads("storeDir")
    def store_dir(self) -> Str:
        """The store directory this object belongs to.

        `/nix/store` for almost everything, a chroot store included:
        this is the LOGICAL prefix baked into every path the store
        holds, and `real_path` answers where the bytes actually are.

        On the wire because `nix::UnkeyedValidPathInfo` cannot be
        built without one, so the far side cannot rebuild a PathInfo
        without it. Upstream supports relocatable store objects, and
        different objects may carry different directories."""

    @reads("narHash")
    def nar_hash(self) -> "Hash":
        """The hash of the path's NAR serialisation.

        A `Hash`, so the algorithm is a field rather than a prefix:
        `info.nar_hash().algorithm()` answers without splitting a
        string, and `str(info.nar_hash())` still prints the
        `sha256:<base32>` that `nix path-info` does."""

    @reads("narSize")
    def nar_size(self) -> U64:
        """The size of that NAR in bytes. Not the size on disk."""

    @reads("deriver")
    def deriver(self) -> "StorePath | None":
        """The .drv that built this, or None.

        None is a real answer, not a gap: a path added straight to the
        store was not built by anything."""

    def registration_time(self) -> "I64 | None":
        """When the store learnt about this path, as a Unix time, or
        None when it does not know.

        Upstream spells "unknown" as 0, which is also a real Unix
        time. None is the honest reading, and it is what crosses."""
        Cxx("""
if (!self.registrationTime)
    return std::nullopt;
return static_cast<std::int64_t>(self.registrationTime);
        """)

    @reads("ultimate")
    def ultimate(self) -> Bint:
        """Whether this store built it itself, as opposed to receiving
        it from a substituter or an import."""

    @reads("ca")
    def ca(self) -> "ContentAddress | None":
        """How this path's content addresses itself, or None.

        Present for a path ADDED to the store. None for one that was
        BUILT: an input-addressed output is named after the derivation
        that made it, not after its own bytes, so there is nothing to
        address by.

        None rather than an empty one: the two are different answers,
        and the wire carries them both across (tasks/048)."""

    @reads("references")
    def references(self) -> "list[StorePath]":
        """The store paths this one points at, its own included when
        it does.

        This is what makes a store path a graph rather than a name: a
        closure is the transitive reading of this field. Nix scans the
        bytes for them at add time, so a path added from a directory
        of plain text has none.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""

    @reads("sigs")
    def sigs(self) -> "list[Signature]":
        """Who vouched for this path.

        Empty for a path this store added itself: a signature says a
        path came from somewhere and arrived intact, and a local add
        travelled nowhere.

        Sorted, because Nix keeps them in a set and the order is that
        set's."""

    # --- the wire's other half, where it belongs ---------------------

    # `local-keys.hh` for Signature::parse. path-info.hh reaches it
    # through signer.hh today, and a body that leans on somebody
    # else's include is a body that breaks on an upstream tidy-up.
    @needs("nix/util/signature/local-keys.hh")
    def _from_parts() -> "PathInfo":
        """Rebuild one from the parts that crossed.

        The emitter writes the signature - one parameter per accessor
        above, in that order - so this can only consume what `_parts`
        sent. It cannot disagree about what crosses or in what order.

        It CAN drop a part, which is the whole reason
        `test_every_wire_value_survives_its_own_round_trip` exists. A
        record's `_from_parts` was aggregate initialisation and could
        not miss a field; this is a bijection a person wrote, and one
        that never assigns `ultimate` compiles and zero-initialises it
        (tasks/056).

        Two things make it longer than an aggregate.
        nix::ValidPathInfo has a virtual base, so it is not an
        aggregate at all, and its only constructor takes an
        UnkeyedValidPathInfo - which in turn needs the store
        directory.

        There was a third: `nar_hash`, `ca` and `sigs` used to arrive
        as TEXT and were parsed back here, with `Hash::parseAnyPrefixed`,
        `ContentAddress::parse` and `Signature::parse`. They arrive as
        themselves now, so three parses and their failure modes are
        gone - a rendering that could not be re-read was a bug this
        body could have; assigning a value it was handed is not."""
        Cxx("""
nix::UnkeyedValidPathInfo u{store_dir, nar_hash};
u.deriver = deriver;
u.references = as_set<nix::StorePathSet>(references);
u.registrationTime = registration_time.value_or(0);
u.narSize = nar_size;
u.ultimate = ultimate;
u.sigs = as_set<std::set<nix::Signature>>(sigs);
u.ca = ca;
return nix::ValidPathInfo{path, std::move(u)};
        """)
