# A vocabulary checked against its C++ enum

**MOSTLY DONE.** Two vocabularies ship today and neither is checked against
the C++ enum it stands for. Nix has more of them coming, so the
check belongs in the emitter rather than in a reviewer's eye.

## What is declared today, and what is behind it

`decl/words.py` declares two `@words` vocabularies. Both have a real
C++ enum upstream, and the declaration says nothing about it:

    HashAlgorithm         nix/util/hash.hh:14
      enum struct HashAlgorithm : char { MD5 = 42, SHA1, SHA256, SHA512, BLAKE3 };
    ContentAddressMethod  nix/store/content-address.hh:33
      struct ContentAddressMethod { enum struct Raw { Flat, NixArchive, Git, Text }; ... }

So `@words` is not "a vocabulary with no C++ behind it" after all.
That was true of the SHAPE - a member IS the string a parser takes -
and false of the FACT. Upstream owns the list; we restate it.

More are waiting, and this is why the marker is worth having rather
than a one-off:

    BuildMode                   store-api.hh:53   bmNormal, bmRepair, bmCheck
    BuildResultSuccessStatus    build-result.hh:22
    BuildResultFailureStatus    build-result.hh:35
    TrustedFlag                 store-api.hh:55
    GCAction                    gc-store.hh:30
    FileIngestionMethod         file-content-address.hh:90

## The hand-written mapping this leaves in a declaration

`decl/hash.py` carries it, and it is the direction that has no
generated form:

    def algorithm(self) -> HashAlgorithm:
        Cxx("return std::string(nix::printHashAlgo(self.algo));")

`@reads("algo")` is what that line wants to be. It cannot be, because
a vocabulary has a TO-C++ conversion and no FROM-C++ one: `parsed_by`
names `nix::parseHashAlgo` and the emitter writes it at each site,
and nothing goes the other way. So every enum-valued RETURN is a
hand-written body.

## The two gates, measured before anything was built

Compiled on 2026-09-01, `.scratchpad/enum/proof.cc`, gcc 15.3.0,
`-std=c++23 -Werror=switch -Werror=return-type`. Upstream's own
shape: `enum struct Algo : char { MD5 = 42, SHA1, SHA256 };`

A defaultless switch over the enum, one case removed:

    proof.cc:10:12: error: enumeration value 'SHA256' not handled in
    switch [-Werror=switch]

An `nb::enum_`-shaped `.value("MD5", Algo::MD5).value(...)` chain,
one enumerator left out of the chain:

    COMPILES. No diagnostic.

That is the whole comparison. A switch is a compile-time
exhaustiveness check and a chain of method calls is not, because
`.value()` is a runtime call and C++ has nothing to check it against.

So the gate catches:

    upstream ADDS an enumerator    -Werror=switch on the defaultless switch
    upstream RENAMES or REMOVES    generated code names nix::HashAlgorithm::MD5
    our SPELLING drifts            round-trip through upstream's own
                                   parse/render, at test time
    upstream REORDERS or RENUMBERS not checked, and not API - which is
                                   the argument against an IntEnum surface

## Why not an IntEnum surface

`MD5 = 42` is upstream's evidence against it. The numbering is an
implementation detail of a header, the wire carries a string, and
`words.py` is emitted as a StrEnum module. An IntEnum would publish
the number and change the wire to gain nothing the switch does not
already give.

## Shape being spiked

Keep the StrEnum surface exactly as it is. Add the C++ enum to the
declaration, and let the emitter write a `type_caster` for it, the
way `tasks/067` did for the union - so a signature names
`nix::HashAlgorithm`, `@reads("algo")` works, and the caster's
from-C++ half IS the defaultless switch.

## Built, and then broken three ways

`@words` gained `enumerated=Enumerated("nix::HashAlgorithm")`. The
Python surface did not change: still a StrEnum, still `str` on the
wire, still `str` in every nanobind signature. What changed is that
the emitter now writes the direction `parsed_by` never had.

`hash.cpp`, emitted:

    namespace huggorm {

    /** A HashAlgorithm, as the word Python has. */
    inline std::string as_word(nix::HashAlgorithm value)
    {
        switch (value) {
        case nix::HashAlgorithm::MD5: return "md5";
        case nix::HashAlgorithm::SHA1: return "sha1";
        case nix::HashAlgorithm::SHA256: return "sha256";
        case nix::HashAlgorithm::SHA512: return "sha512";
        case nix::HashAlgorithm::BLAKE3: return "blake3";
        }
        throw nix::Error("unknown nix::HashAlgorithm");
    }

    }  // namespace huggorm

and the declaration lost a mapping. `Hash.algorithm` was

    Cxx("return std::string(nix::printHashAlgo(self.algo));")

and is now `@reads("algo")`, which emits
`return huggorm::as_word(self.algo);`.

### P1: a word removed from the declaration

`SHA512` deleted from `decl/words.py`, `nix build --file .
huggorm-bindings`:

    huggorm_bindings/hash.cpp:14:12: error: enumeration value 'SHA512'
    not handled in switch [-Werror=switch]

The build FAILS. This is the gate: it is the same diagnostic upstream
adding a sixth algorithm would produce, because a switch cannot tell
the two cases apart.

### P3: the same break, with the flag taken off

`-Werror=switch` removed from `setup.py`, `SHA512` still missing:

    BUILDS. Exit 0.

So the flag is what does the work. `-Wall` was already in the compile
line and was not enough - it makes the missing case a warning, and a
warning in a build that prints thousands of lines is a warning nobody
reads.

### P2: a word MISSPELLED

`SHA256 = "sha256"` changed to `"sha-256"`. It compiles, because the
enumerator is unchanged and only the string literal moved. The
failure comes at run time, from libstore:

    huggorm_bindings.errors.UsageError: error: unknown hash algorithm
    'sha-256', expect 'blake3', 'md5', 'sha1', 'sha256', or 'sha512'

**And this half was already covered, incidentally.** The failure above
is the existing smoke test inside the `huggorm-generated` build, not
the new test file. Anything that hands a word to libstore checks that
word's spelling as a side effect.

What it does NOT cover is every word. Counted: `SHA256` and `SHA1`
reach `nix::parseHashAlgo` from the suite today. `MD5`, `SHA512` and
`BLAKE3` reach it from nowhere, so three of five spellings were
unchecked. `tests/test_words.py` parametrises over the vocabulary, so
the coverage is by construction rather than by coincidence.

## What the two gates each hold

    upstream ADDS an enumerator      P1. compile time, hard failure.
    upstream RENAMES or REMOVES one  the switch names it. compile time.
    a word we declare is MISSPELLED  P2. test time, through upstream's
                                     own parser.
    a word is GATED by a feature     test time. `blake3` is refused
                                     because the binding hands the
                                     string to upstream rather than
                                     mapping it - goal 1.
    upstream REORDERS or RENUMBERS   not checked, and not API.

## What is NOT done

`ContentAddressMethod` is the second vocabulary with an enum behind
it, and it is a harder shape: `nix::ContentAddressMethod` is a STRUCT
holding `Raw raw`, and one word is spelled differently
(`NAR` is `NixArchive`). `Enumerated` carries `spelled` for the second
half; the first half is untested.

`BuildMode`, `BuildResultSuccessStatus`, `BuildResultFailureStatus`,
`TrustedFlag`, `GCAction` and `FileIngestionMethod` are the queue
behind that.
