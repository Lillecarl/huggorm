# A vocabulary checked against its C++ enum

**OPEN.** Two vocabularies ship today and neither is checked against
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

## What is NOT done

Everything below the proof. The measurements above are from a scratch
file, not from the emitter.
