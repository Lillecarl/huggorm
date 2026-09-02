"""
Nix's own words, as StrEnums a caller can type.

A vocabulary is not a binding, and that is why these live together
rather than one per header. There is nothing to compile: a member IS
the string libstore parses, so `Store.add_to_store` hands one straight
to `ContentAddressMethod::parse` and this translates nothing. What it
buys is that an editor offers the words and a typo fails before the
call.

Collected here because the split that matters is COMPILES or does not.
`decl/hash.py` binds nix::Hash and `decl/content_address.py` binds
nix::ContentAddress, one header each; a StrEnum compiles to nothing
and has no extension to live in, so a file named after either header
would have been the wrong home for the other's.

A C++ enum IS behind both of these, and `enumerated=` says which
(tasks/070). That does not make either one a binding - the type
Python sees is still a StrEnum and the wire still carries the string.
What it buys is that the compiler holds the LIST and a test holds the
SPELLING, where before this the list was two people reading two
repositories.
"""

from huggorm_dsl.declare import Enumerated, Wrap, header, words


@header("nix/store/content-address.hh")
@words(parsed_by="nix::ContentAddressMethod::parse",
       enumerated=Enumerated(
           "nix::ContentAddressMethod::Raw",
           # EVERY word, not just the one that reads differently.
           # A vocabulary spells its words the way Python spells a
           # constant and upstream spells this enum the way C++
           # spells a type, so the default - the word's own name -
           # is right for `nix::HashAlgorithm` by coincidence and
           # wrong for all four of these.
           #
           # `nar` is the one that is not a case difference:
           # upstream calls the method NixArchive, and the word is
           # what a store URI and a .narinfo carry. Both names are
           # right and neither derives the other.
           spelled={
               "FLAT": "Flat",
               "NAR": "NixArchive",
               "GIT": "Git",
               "TEXT": "Text",
           },
           # A struct with one member, and a method that answers
           # one answers the struct.
           wrapped=Wrap("nix::ContentAddressMethod", holds="raw"),
       ))
class ContentAddressMethod:
    """How the hash that names a store path is computed.

    A StrEnum, so a member IS the string libstore parses. Passing
    `ContentAddressMethod.FLAT` and passing `"flat"` are the same
    call, which is what keeps this a convenience rather than a layer.
    """

    FLAT = "flat"
    """The contents of a single file, hashed exactly as they are."""

    NAR = "nar"
    """The Nix Archive serialisation of a file system object. The
    default for `nix-store --add`, and the only one that can describe
    a directory."""

    GIT = "git"
    """Git's own tree hashing.

    NOT behind an experimental feature at this entry point, which is
    worth stating because it looks like it should be.
    `ContentAddressMethod::parse` reaches `parseFileIngestionMethod`,
    which takes `git` with no check. `parsePrefix` is the one that
    requires `Xp::GitHashing`, and nothing here calls it."""

    TEXT = "text"
    """Flat hashing, with references recorded. What `builtins.toFile`
    produces."""


@header("nix/util/hash.hh")
@words(parsed_by="nix::parseHashAlgo",
       enumerated=Enumerated("nix::HashAlgorithm"))
class HashAlgorithm:
    """The digest used to content-address a store object.

    Lives beside ContentAddressMethod rather than in a module of its
    own: it comes from `nix/util/hash.hh`, but the only binding that
    takes one takes it as the other's companion. It moves when
    something else needs it.
    """

    MD5 = "md5"
    SHA1 = "sha1"
    SHA256 = "sha256"
    """Nix's default, and what every store path in the wild uses."""

    SHA512 = "sha512"
    BLAKE3 = "blake3"
    """Behind the `blake3-hashes` experimental feature - the word is
    known, the feature is off until enabled."""


@header("nix/store/store-api.hh")
@words(
    # No `parsed_by`, and that is a fact about upstream rather than an
    # omission. `nix::BuildMode` has no parser and no rendering: it
    # crosses Nix's own worker protocol as an integer, so these three
    # words are this binding's own. The emitter writes both
    # directions, and the read-back one is still the switch that
    # checks the list.
    enumerated=Enumerated(
        "nix::BuildMode",
        spelled={"NORMAL": "bmNormal",
                 "REPAIR": "bmRepair",
                 "CHECK": "bmCheck"},
    ),
)
class BuildMode:
    """What a build is FOR, beyond making the outputs valid.

    A vocabulary rather than a flag, because upstream is an enum and
    the three answers are not two booleans: repairing and checking
    both re-run a builder whose outputs are already there, and they
    do opposite things with the result.
    """

    NORMAL = "normal"
    """Make the outputs valid, and stop as soon as they are.

    A target that is already valid is a no-op, and one that can be
    substituted is fetched rather than built."""

    REPAIR = "repair"
    """Rebuild an output whose contents no longer hash to its name,
    and REPLACE it.

    For a store somebody has edited or a disk that has corrupted one.
    The daemon refuses this from an untrusted client."""

    CHECK = "check"
    """Rebuild an output that is already valid and compare, without
    replacing it.

    This is how a derivation is shown to be non-reproducible: the
    second build's outputs are compared with the first's and a
    difference is an error."""


@header("nix/store/build-result.hh")
@words(
    # No `parsed_by`, the same way `BuildMode` has none. Upstream
    # parses neither status enum from a string: both cross Nix's own
    # protocol as an integer, so these words are this binding's own
    # and the emitter writes both directions.
    enumerated=Enumerated(
        "nix::BuildResultSuccessStatus",
        # Every word, because upstream spells an enumerator the way
        # C++ spells a type and a word is spelled the way a store URI
        # spells one. Neither derives the other.
        spelled={
            "BUILT": "Built",
            "SUBSTITUTED": "Substituted",
            "ALREADY_VALID": "AlreadyValid",
            "RESOLVES_TO_ALREADY_VALID": "ResolvesToAlreadyValid",
        },
    ),
)
class BuildSuccessStatus:
    """How a target came to be valid.

    Four ways, and they are not degrees of the same thing: one of
    them ran a builder and three of them did not. A caller measuring
    a cache asks this and nothing else.
    """

    BUILT = "built"
    """A builder ran here and produced the outputs."""

    SUBSTITUTED = "substituted"
    """The outputs came from a binary cache, and no builder ran."""

    ALREADY_VALID = "already-valid"
    """The outputs were in the store before the call. Nothing ran and
    nothing was fetched."""

    RESOLVES_TO_ALREADY_VALID = "resolves-to-already-valid"
    """The derivation resolved - its input derivations were replaced
    by the paths they built - and THAT derivation's outputs were
    already valid.

    Only reachable with `ca-derivations`, because resolving is what a
    content-addressed derivation does before it is built."""


@header("nix/store/build-result.hh")
@words(
    enumerated=Enumerated(
        "nix::BuildResultFailureStatus",
        spelled={
            "PERMANENT_FAILURE": "PermanentFailure",
            "INPUT_REJECTED": "InputRejected",
            "OUTPUT_REJECTED": "OutputRejected",
            "TRANSIENT_FAILURE": "TransientFailure",
            "CACHED_FAILURE": "CachedFailure",
            "TIMED_OUT": "TimedOut",
            "MISC_FAILURE": "MiscFailure",
            "DEPENDENCY_FAILED": "DependencyFailed",
            "LOG_LIMIT_EXCEEDED": "LogLimitExceeded",
            "NOT_DETERMINISTIC": "NotDeterministic",
            "NO_SUBSTITUTERS": "NoSubstituters",
            "HASH_MISMATCH": "HashMismatch",
        },
    ),
)
class BuildFailureStatus:
    """Why a target did not become valid.

    Separate from BuildSuccessStatus rather than one list of sixteen
    words, and that is a decision (tasks/071). Upstream's own comment
    on both enums says "Names must be disjoint with" the other, which
    WOULD license one Python vocabulary over two C++ switches. Two
    reasons not to take it. `Enumerated` names one C++ enum, so a
    merged list would have to say which of two `from_word` a word
    belongs to, and the answer is the arm - which the caller already
    has. And the arms carry different things: a success has outputs
    and a failure has a message, so a caller branches whatever the
    word list looks like.

    The disjointness is still upstream's invariant, so a test asserts
    it rather than this assuming it.
    """

    PERMANENT_FAILURE = "permanent-failure"
    """The builder ran and failed. Running it again will fail again."""

    INPUT_REJECTED = "input-rejected"
    """A remote builder would not accept one of the inputs."""

    OUTPUT_REJECTED = "output-rejected"
    """The build produced an output the store will not take."""

    TRANSIENT_FAILURE = "transient-failure"
    """The build failed for a reason that may not hold next time -
    a network, a disk, a machine that went away."""

    CACHED_FAILURE = "cached-failure"
    """No longer used, in upstream's own words. Kept because the
    enumerator is still there and the switch names every one."""

    TIMED_OUT = "timed-out"
    """The builder passed its timeout and was killed."""

    MISC_FAILURE = "misc-failure"
    """Anything else. The default a BuildError carries when nothing
    set a narrower one."""

    DEPENDENCY_FAILED = "dependency-failed"
    """This target never ran, because something it needs failed."""

    LOG_LIMIT_EXCEEDED = "log-limit-exceeded"
    """The builder wrote more log than the limit allows, and was
    killed for it."""

    NOT_DETERMINISTIC = "not-deterministic"
    """A check build produced different outputs from the first one.
    Only reachable with BuildMode.CHECK."""

    NO_SUBSTITUTERS = "no-substituters"
    """Nothing could supply the outputs, and this call was not allowed
    to build them."""

    HASH_MISMATCH = "hash-mismatch"
    """A fixed-output derivation produced a different hash from the
    one it declared.

    Upstream calls this a certain type of OUTPUT_REJECTED and turns it
    back into one before serialising, because the protocols do not
    know this word. So a result read over a daemon connection may say
    `output-rejected` where a local one says this."""


@header("nix/store/store-api.hh")
@words(
    # No `parsed_by`, and this one was CHECKED rather than assumed
    # from BuildMode's case. Upstream has no string parser and no
    # renderer for it: the only conversions in libstore are the JSON
    # pair in `misc.cc`, and they read and write a BOOLEAN. So these
    # two words are this binding's own and the emitter writes both
    # directions.
    enumerated=Enumerated(
        "nix::TrustedFlag",
        # Both, because neither default is right: upstream spells an
        # enumerator the way C++ spells a type, and a word is
        # kebab-case.
        spelled={"TRUSTED": "Trusted", "NOT_TRUSTED": "NotTrusted"},
    ),
)
class TrustedFlag:
    """Whether a store trusts the client talking to it.

    Upstream's own note is worth repeating, because the name reads the
    other way round at first: this is whether the STORE trusts US. The
    `trusted` setting on a store is the opposite question - whether we
    trust the store - and the two have no bearing on each other.

    An UNSCOPED enum upstream, and over `bool` at that:
    `enum TrustedFlag : bool { NotTrusted = false, Trusted = true }`.
    Neither fact reaches this declaration. `nix::TrustedFlag::Trusted`
    is how upstream itself writes one, so the emitter's usual
    `{cxx}::{word}` spelling is right with nothing said here.

    Two words rather than a bool on the Python side, and that is the
    point of declaring it: the answer is a THREE-way one.
    `Store.is_trusted_client` says None when the store cannot tell,
    and a bool would have to pick a side for that.
    """

    TRUSTED = "trusted"
    """The store accepts what this client asks of it."""

    NOT_TRUSTED = "not-trusted"
    """The store limits what this client may do.

    An untrusted client cannot repair paths, cannot add signatures of
    its own, and cannot import a path claiming a signature it does not
    have. A daemon decides this from the connecting user."""


@header("nix/store/gc-store.hh")
@words(
    # No `parsed_by`, CHECKED rather than assumed. The only
    # conversions upstream are the worker protocol's, and they read
    # and write a NUMBER - `readNum<unsigned>` and a switch back
    # (`worker-protocol.cc:83`). Nothing turns a string into one, so
    # both directions are this binding's own.
    enumerated=Enumerated(
        "nix::GCAction",
        # Upstream prefixes every enumerator with the type it is
        # already inside - `gcDeleteDead` on a `GCAction`. The word
        # drops the prefix, so the spelling has to be stated.
        spelled={
            "RETURN_LIVE": "gcReturnLive",
            "RETURN_DEAD": "gcReturnDead",
            "DELETE_DEAD": "gcDeleteDead",
            "DELETE_SPECIFIC": "gcDeleteSpecific",
        },
    ),
)
class GCAction:
    """What a garbage collection should DO.

    Four answers, and only two of them delete anything. That is the
    reason this is a word rather than a flag: `nix-store --gc
    --print-dead` and `nix-store --gc` are the same call with this
    field changed, and a caller who wants the list must not be one
    typo away from the deletion.

    A scoped enum upstream - `enum class GCAction` - so
    `nix::GCAction::gcDeleteDead` is how a body spells one, which is
    the emitter's usual `{cxx}::{word}` with the spelling above.
    """

    RETURN_LIVE = "return-live"
    """Answer with the paths reachable from the roots, and delete
    nothing.

    The closure of everything something still points at. `nix-store
    --gc --print-live`."""

    RETURN_DEAD = "return-dead"
    """Answer with the paths NOT reachable from the roots, and delete
    nothing.

    What a collection would remove, without removing it. `nix-store
    --gc --print-dead`."""

    DELETE_DEAD = "delete-dead"
    """Delete everything the roots do not reach.

    Upstream's default, and what `nix-store --gc` does with no other
    argument."""

    DELETE_SPECIFIC = "delete-specific"
    """Delete the listed paths, and only those that nothing reaches.

    The list is `GCOptions.paths_to_delete`. A path something still
    points at survives, so this asks rather than orders - `nix-store
    --delete` is the same operation and fails loudly on a path with
    referrers."""
