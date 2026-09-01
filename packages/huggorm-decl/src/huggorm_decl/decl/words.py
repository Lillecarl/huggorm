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
