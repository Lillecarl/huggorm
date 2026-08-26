"""
How a store object is content-addressed: `nix/store/content-address.hh`.

A vocabulary, not a binding. There is nothing to compile: the values
are Nix's words, and `Store.add_to_store` hands one straight to
`ContentAddressMethod::parse`, so this translates nothing. It names
what libstore already accepts, so an editor can offer the words and a
typo fails before the call.
"""

from cythonix_idl.declare import header, words


@header("nix/store/content-address.hh")
@words(parsed_by="nix::ContentAddressMethod::parse")
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
    """Git's own tree hashing. Behind the `git-hashing` experimental
    feature: libstore knows the word and refuses the feature until it
    is enabled."""

    TEXT = "text"
    """Flat hashing, with references recorded. What `builtins.toFile`
    produces."""


@header("nix/util/hash.hh")
@words(parsed_by="nix::parseHashAlgo")
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
