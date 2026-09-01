"""
A vocabulary, checked against the C++ enum it stands for (tasks/070).

Two halves, and neither one covers the other.

The COMPILER holds the list. Every emitted `as_word` is a switch over
the Nix enum with no `default:`, built with `-Werror=switch`, so a
word this repo does not know about is a build failure rather than a
silent fallthrough. Nothing here can test that, because a binding
that failed to build is not importable.

This file holds the SPELLING, which the compiler cannot check: our
`"sha256"` and upstream's `"sha256"` are two string literals in two
repositories, and only a round trip through libstore shows they are
the same word. A word goes in through upstream's own parser and comes
back out through our switch.

The third thing it holds is the harder one, and it is goal 1: a
binding may not be more permissive than the C++ it binds. `blake3` is
a word libstore KNOWS and refuses until an experimental feature is
on, so a binding that mapped the string itself would accept it where
libstore does not.
"""

import re

import pytest

from huggorm_bindings import Hash, HashAlgorithm
from huggorm_bindings.errors import NixError

# Words libstore parses only with an experimental feature on. The
# feature is off in this suite, so the round trip below cannot reach
# them - and that they are unreachable IS the goal-1 test further
# down, rather than an exclusion this file is apologising for.
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


@pytest.mark.parametrize(
    "word", [w for w in HashAlgorithm if w not in GATED])
def test_a_declared_word_comes_back_as_itself(word: HashAlgorithm) -> None:
    """The round trip: our word in, libstore's enum, our word out.

    `Hash(word, ...)` hands the string to `nix::parseHashAlgo`, and
    `algorithm()` reads the enumerator back through the emitted
    switch. Both halves have to agree with upstream for this to
    return what went in."""
    assert a_hash(word).algorithm() == word


@pytest.mark.parametrize(
    "word", [w for w in HashAlgorithm if w not in GATED])
def test_upstream_prints_the_same_word_we_declare(
        word: HashAlgorithm) -> None:
    """libstore's own rendering, read out of its own message.

    The test above compares our switch with our word list, which
    would still pass if BOTH said `sha-256`. This compares our word
    with UPSTREAM's, because the sentence libstore refuses with is
    built by `nix::printHashAlgo`."""
    with pytest.raises(NixError, match=f"a {word} digest is"):
        Hash(word, b"")


@pytest.mark.parametrize("word,feature", sorted(GATED.items()))
def test_a_gated_word_is_refused_the_way_libstore_refuses_it(
        word: HashAlgorithm, feature: str) -> None:
    """Goal 1: the binding is not more permissive than the C++.

    `blake3` is in the vocabulary because libstore knows the word.
    Parsing it requires an experimental feature, and the binding hands
    the string to `nix::parseHashAlgo` rather than mapping it, so the
    refusal is upstream's and says which feature to turn on."""
    with pytest.raises(NixError, match=feature):
        Hash(word, b"")
