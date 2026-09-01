"""
A vocabulary, checked against the C++ enum it stands for (tasks/070).

Two halves, and neither one covers the other.

The COMPILER holds the LIST. Every emitted `as_word` is a switch over
the Nix enum with no `default:`, built with `-Werror=switch`, so a
word this repo does not know about is a build failure. Nothing here
can test that, because a binding that failed to build is not
importable.

This file holds the SPELLING, which the compiler cannot check: our
`"sha256"` and upstream's `"sha256"` are two string literals in two
repositories. A word goes in through upstream's own parser and comes
back out through our switch, so both halves have to agree with
upstream for it to survive the trip.

The third thing it holds is goal 1: a binding may not be more
permissive than the C++ it binds. `blake3` and `git` are words
libstore KNOWS and refuses until an experimental feature is on, so a
binding that mapped the string itself would accept them where
libstore does not.
"""

import re
from collections.abc import Callable
from enum import StrEnum
from typing import Any

import pytest

from huggorm_bindings import ContentAddress, Hash, HashAlgorithm
from huggorm_bindings import ContentAddressMethod as CA
from huggorm_bindings.errors import NixError

# Words the parser this binding uses refuses without an experimental
# feature. Off in this suite, so the round trip cannot reach them -
# and that they are unreachable IS the goal-1 test at the bottom,
# rather than an exclusion this file is apologising for.
#
# `git` is NOT here, and finding that out is what this test did.
# `nix::ContentAddressMethod::parse` takes it with no check at all:
# only `parsePrefix` requires Xp::GitHashing, and that is a different
# entry point. So the word is reachable and round-trips like the rest.
GATED = {HashAlgorithm.BLAKE3: "blake3-hashes"}


def a_hash(word: HashAlgorithm) -> Hash:
    """One Hash of the named algorithm, with no table of digest sizes.

    A sha256 is 32 bytes and a sha512 is 64, and writing that down
    here would be a second list to keep in step with upstream - which
    is the fault this whole file exists to remove.

    So libstore is asked. It refuses an empty digest and says how many
    bytes it wanted, and the answer builds the hash."""
    with pytest.raises(NixError) as refused:
        Hash(word, b"")
    size = re.search(r"digest is (\d+) bytes", str(refused.value))
    assert size is not None, f"libstore changed the message: {refused.value}"
    return Hash(word, bytes(int(size.group(1))))


def a_method(word: CA) -> CA:
    """One ContentAddressMethod, there and back.

    `ContentAddress(...)` hands the word to
    `nix::ContentAddressMethod::parse`, and `method()` reads the
    enumerator back through the emitted switch."""
    return ContentAddress(word, a_hash(HashAlgorithm.SHA256)).method()


# Every vocabulary with a C++ enum behind it: the round trip that
# reaches it, and the bare PROBE that hands the word to libstore and
# does nothing else.
#
# Two callables rather than one, because the round trip cannot serve
# a word libstore refuses. `a_hash` reads the digest size out of the
# refusal, so a word that fails for a DIFFERENT reason arrives as a
# confusing assertion about the message rather than as the refusal
# itself. The probe is what the goal-1 test needs.
TRIPS: dict[str, tuple[type[StrEnum],
                       Callable[[Any], Any],
                       Callable[[Any], Any]]] = {
    "HashAlgorithm": (HashAlgorithm,
                      lambda w: a_hash(w).algorithm(),
                      lambda w: Hash(w, b"")),
    "ContentAddressMethod": (CA,
                             a_method,
                             lambda w: ContentAddress(
                                 w, a_hash(HashAlgorithm.SHA256))),
}
ROUND_TRIP = [(name, word)
              for name, (vocab, _, _) in TRIPS.items()
              for word in vocab if word not in GATED]


@pytest.mark.parametrize("vocabulary,word", ROUND_TRIP)
def test_a_declared_word_comes_back_as_itself(
        vocabulary: str, word: StrEnum) -> None:
    """Our word in, libstore's enumerator, our word out.

    This catches a misspelling in either direction. Upstream's parser
    refuses a word it does not know, and our switch answers with the
    word the declaration wrote - so the two have to be the same word
    for this to return what went in."""
    assert TRIPS[vocabulary][1](word) == word


@pytest.mark.parametrize(
    "word", [w for w in HashAlgorithm if w not in GATED])
def test_upstream_prints_the_same_word_we_declare(
        word: HashAlgorithm) -> None:
    """libstore's own rendering, read out of its own message.

    Not the same check as the round trip above, and the difference is
    canonicality. A parser that took a synonym would let a
    non-canonical word round-trip cleanly, and the word is what
    crosses the wire and what a `.narinfo` carries. The sentence
    libstore refuses an empty digest with is built by
    `nix::printHashAlgo`, so it is upstream's own spelling.

    HashAlgorithm only. `nix::ContentAddressMethod` renders as a
    PREFIX - `r:`, `text:` - rather than as the word, so upstream
    offers nothing to compare there and the round trip is what there
    is."""
    with pytest.raises(NixError, match=f"a {word} digest is"):
        Hash(word, b"")


@pytest.mark.parametrize("word,feature", sorted(GATED.items()))
def test_a_gated_word_is_refused_the_way_libstore_refuses_it(
        word: StrEnum, feature: str) -> None:
    """Goal 1: the binding is not more permissive than the C++.

    The word is in the vocabulary because libstore knows it. Parsing
    it requires an experimental feature, and the binding hands the
    string to upstream rather than mapping it, so the refusal is
    upstream's and says which feature to turn on.

    One word today. `git` was the second candidate and is not one:
    see GATED."""
    with pytest.raises(NixError, match=feature):
        TRIPS[type(word).__name__][2](word)


def test_the_two_build_statuses_keep_upstream_disjoint() -> None:
    """Upstream's own invariant, asserted rather than assumed.

    `BuildResultSuccessStatus` and `BuildResultFailureStatus` each
    carry the comment "Names must be disjoint with" the other. That
    disjointness is the licence to publish one Python vocabulary of
    sixteen words over two C++ switches, and tasks/071 declined it -
    two vocabularies, because `Enumerated` names one C++ enum and the
    arms carry different things.

    Declining it does not make the invariant stop mattering. If
    upstream ever adds a name to one enum that the other already has,
    a merged list becomes impossible and this repo should find out
    from a test rather than from the day somebody tries.
    """
    from huggorm_bindings import BuildFailureStatus, BuildSuccessStatus

    won = {w.name for w in BuildSuccessStatus}
    lost = {w.name for w in BuildFailureStatus}
    assert not won & lost, f"upstream's names collide: {won & lost}"

    said = {w.value for w in BuildSuccessStatus}
    meant = {w.value for w in BuildFailureStatus}
    assert not said & meant, f"our words collide: {said & meant}"
