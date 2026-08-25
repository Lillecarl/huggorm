"""
How a store object is content-addressed: `nix/store/content-address.hh`.

Plain Python, like `errors.py`, because there is nothing to compile. A
StrEnum works perfectly well inside a .pyx - Cython runs the metaclass
and the members come out real - but compiling a vocabulary into a
shared object buys nothing, and a .py module needs no generated stub
for a typechecker to read it (the stubs are `partial` exactly so a
hand-written module stays visible).

The values are Nix's words, not ours. `Store.add_to_store` hands the
string straight to `ContentAddressMethod::parse`, so this class does
not translate anything - it names what libstore already accepts, so an
editor can offer the four and a typo fails before the call.
"""

from enum import StrEnum


class ContentAddressMethod(StrEnum):
    """How the hash that names a store path is computed.

    A StrEnum, so a member IS the string libstore parses. Passing
    `ContentAddressMethod.FLAT` and passing `"flat"` are the same call,
    which is what keeps this a convenience rather than a layer.
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


class HashAlgorithm(StrEnum):
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
