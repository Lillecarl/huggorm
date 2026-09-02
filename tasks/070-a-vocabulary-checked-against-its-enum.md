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

## The second vocabulary, which is the harder shape

`nix::ContentAddressMethod` is a STRUCT holding one `Raw raw` member,
so C++ hands over the struct and the switch has to reach inside.
`Enumerated` gained `wrapped=Wrap("nix::ContentAddressMethod",
holds="raw")` for that, reusing the `Wrap` the unions already had -
one fact, "how a C++ type holds a value the Python surface names
directly", and two shapes needing it.

It emits:

    inline std::string as_word(nix::ContentAddressMethod value)
    {
        switch (value.raw) {
        case nix::ContentAddressMethod::Raw::Flat: return "flat";
        case nix::ContentAddressMethod::Raw::NixArchive: return "nar";
        ...

and `ContentAddress.method` lost its own `Cxx` body - the second
hand-written mapping this removes.

### P4: the default enumerator spelling was WRONG, and the build said so

`Enumerated` defaults an enumerator to the word's own name, and that
is right for `nix::HashAlgorithm` BY COINCIDENCE: upstream happens to
spell it `MD5`, `SHA1`, `SHA256`. `Raw` is spelled `Flat`, `Git`,
`Text`, so three of four defaults were wrong. Emitted and compiled
without noticing:

    error: 'FLAT' is not a member of 'nix::ContentAddressMethod::Raw'
    error: 'GIT' is not a member of ...; did you mean 'Git'?
    error: 'TEXT' is not a member of ...
    error: enumeration value 'Flat' not handled in switch [-Werror=switch]
    error: enumeration value 'Git' not handled in switch
    error: enumeration value 'Text' not handled in switch

This is the REMOVE-or-RENAME half of the gate, arriving by accident
and firing exactly as designed - both halves at once, and gcc naming
the fix. The default stays, because a wrong default cannot reach a
built binding.

`spelled` now names all four rather than only `NAR`. Three are case,
one is not: upstream calls the NAR method `NixArchive`, and `nar` is
what a store URI and a `.narinfo` carry.

## What the test found in the DECLARATION

`git` was declared as "Behind the `git-hashing` experimental feature:
libstore knows the word and refuses the feature until it is enabled."
The goal-1 test asserted that and FAILED:

    DID NOT RAISE NixError

`nix::ContentAddressMethod::parse` reaches `parseFileIngestionMethod`,
which takes `git` with no check at all. `parsePrefix` is the entry
point that requires `Xp::GitHashing`, and nothing here calls it. The
docstring was believable and wrong; it now says which parser does
what.

`blake3` is gated for real - `parseHashAlgoOpt` calls
`xpSettings.require(Xp::BLAKE3Hashes)` - so one word of the two.

## The wire carries the WORD, and that is decided

Protobuf has native enums, and the objection written above - that an
IntEnum publishes a numbering that is not API - does NOT apply to
one. A proto enum's numbers would be OURS, assigned by the emitter,
not `MD5 = 42` from a header. That argument was aimed at the wrong
target and is withdrawn.

**Carl's decision, 2026-09-01: strings, with a generated mapping at
both ends.** A word is easier to read off a wire than a magic number
is, and these calls are far too expensive for the difference in bytes
to matter.

The condition is "generated on both sides", and it holds:

    to C++      `parsed_by`, or the emitted `from_word` where
                upstream has no parser
    from C++    the emitted `as_word` switch
    to wire     a StrEnum member IS its string
    from wire   `WireCodec.scalar` answers with the enum CLASS, so a
                word that is not a member raises where it was typed

Two arguments FOR a proto enum are real and were weighed. It is
smaller, and the `.proto` would become a second machine-readable
statement of the vocabulary that a client in another language gets
for free. Against: proto3 does not REJECT an unknown enum number - it
preserves it as an integer - so the wire would stop being where a bad
value is caught, and the numbers would become a frozen contract
(`tasks/022`). Today a bad word is refused by the codec before it
leaves Python, and by libstore's own parser if it gets further.

`BuildMode` is the case that tests the decision rather than restating
it, and `tests/test_remote.py` is where: HashAlgorithm costs nothing
as a string because a member IS what libstore parses, and BuildMode
has no string form upstream at all.

## BuildMode, the first vocabulary whose words are ours

`nix::BuildMode` has no parser and no rendering. It crosses Nix's own
worker protocol as an integer, so `normal`, `repair` and `check` are
named in the declaration and the emitter writes BOTH directions - a
chain for the way in, because C++ cannot switch on a string, and the
usual switch for the way out.

The way-out switch is emitted even though nothing returns a
BuildMode. That is deliberate: the switch IS the gate, and without it
the day upstream adds a fourth mode would pass silently. So
`_vocabularies_used` walks every site rather than the returns.

### What the test refuted, twice

The first version asserted all three modes are accepted. That proves
nothing - a binding that DROPPED the mode would pass it, because
`normal` on an already-valid path is a no-op. The test has to be the
difference.

Then which mode differs was guessed wrong. Measured, on a path that
was ADDED rather than built:

    normal   no-op
    check    no-op        rebuild-and-compare, and there is no
                          derivation to rebuild
    repair   RAISES       "no substituter that can build it" -
                          repairing means REPLACING, and substituting
                          is all that is left

One mode of three is observably different, and one is enough.

## What is NOT done

`GCAction` and `FileIngestionMethod` are the queue, and neither is
just a vocabulary any more:

- **`GCAction`** wants `Store.collect_garbage`, which takes a
  `GCOptions` a caller BUILDS and answers a `GCResults`. Every record
  in this repo today is `@produced(by=...)` - something libstore
  made - so an input record is a shape the DSL does not have. That
  is `tasks/074`, with the name clash it also has.
- **`FileIngestionMethod`** has no binding that takes one.
  `add_to_store` takes `ContentAddressMethod`, and
  `ContentAddressMethod::parse` reaches
  `parseFileIngestionMethod` internally - so the word never appears
  on a signature. It stays queued, and a commit of it alone would
  emit nothing at all.

`TrustedFlag` came off the queue on 2026-09-02, with
`Store.is_trusted_client` as its first user. Details below.

`BuildResultSuccessStatus` and `BuildResultFailureStatus` came off
it on 2026-09-01. The larger question this file named was right:
upstream's `BuildResult` is a sum whose failure arm IS `BuildError`,
an exception class, so declaring it was not just an enum. It took
`tasks/071` and four new things in the codegen. Both vocabularies
are declared, every enumerator `spelled`, and a test asserts the
disjointness upstream documents between them.

The queue is a queue rather than a batch, and this session is why. A
vocabulary emits NOTHING until a binding names one - measured in 071,
where the two status vocabularies added one occurrence of the string
"BuildResult" to the emitted tree and it was inside a docstring. So
each of the three lands with the binding that takes it, and a commit
of one alone would prove nothing.

The two front doors that gained a `BuildMode` re-export line each by
hand are fixed: `tasks/064` is done and both are emitted. A new
vocabulary reaches them with no line written.

## TrustedFlag, 2026-09-02

`Store.is_trusted_client` binds `isTrustedClient`, whose upstream
return is `std::optional<TrustedFlag>`. So the vocabulary landed with
its first user, the way the queue rule says.

**Three answers, not two, and that is why it is a vocabulary rather
than a `bool`.** A store can say yes, no, or nothing at all: an HTTP
binary cache has no notion of who is asking. A `bool` return would
have had to pick a side for the third.

**No `parsed_by`, and it was CHECKED.** BuildMode's case made it
likely and the source settled it: the only conversions upstream are
the JSON pair in `misc.cc`, which read and write a BOOLEAN. There is
no string parser and no renderer, so both words are this binding's
own.

**An unscoped enum over `bool`** - `enum TrustedFlag : bool {
NotTrusted = false, Trusted = true }` - and nothing in the
declaration says so. `nix::TrustedFlag::Trusted` is how upstream
itself writes one, so the emitter's usual `{cxx}::{word}` spelling is
right with no `Wrap` and no special case.

### The optional return needed nothing, and why that is worth keeping

A `Vocab | None` return is a first here, and the expectation was that
it would hit `CXX_OPTIONAL` - the table `_accessor` reads to give a
two-return-path lambda an explicit type. It did not. A METHOD's
return is spelled by `_cxx`, which resolves a words class to
`std::string` and wraps it, so the emitted lambda already says `->
std::optional<std::string>`.

The gap is real and it is in the other emitter: `_accessor` still
reads `CXX_OPTIONAL` by literal string, so a PRODUCED VALUE whose
accessor returns `Vocab | None` would get no return type and fail to
compile. Nothing declares one, so this is a code reading rather than
a measurement. `tasks/075`.

### PERTURBED, three ways

1. **A `spelled` entry removed.** `error: 'NOT_TRUSTED' is not a
   member of 'nix::TrustedFlag'` - the word falls back to the default
   `{cxx}::{word}` spelling, which does not exist. That is 070's own
   P3 gate firing on a new vocabulary.
2. **The WORD removed from the class.** `error: enumeration value
   'NotTrusted' not handled in switch [-Werror=switch]`. This is the
   one worth doing: the enum's underlying type is `bool` with exactly
   two enumerators, and the switch gate had never been shown to hold
   for that shape.
3. **The optional inverted in the C++ body** (`if (flag)` for `if
   (!flag)`): `assert None == <TrustedFlag.TRUSTED: 'trusted'>`. The
   test has teeth on the arm a store actually answers.

### Both arms of the optional are hermetic

Which was not obvious and decided the test. `dummy://` answers
`Trusted` - upstream's comment says it is "incapable of *not*
trusting" - and `ssh://` answers `std::nullopt` unconditionally,
because the legacy SSH protocol has no way to ask. Opening an
`ssh://` store CONNECTS TO NOTHING, so the host in the test does not
exist and the test needs no network and no daemon.

`not-trusted` is the third word and no store reachable from a test
produces it: it takes a daemon that decided from the connecting
user, or the restricted store an evaluation builds internally. The
compile gate is what covers it.

### All four surfaces carry it, which was worth checking

`TrustedFlag | None` is the first OPTIONAL SCALAR return here - a
vocabulary rides the string scalar, and absence rides the synthetic
one-field oneof from `tasks/048`. Two gated pieces, in a composition
nothing had used, and an unrepresentable return is REPORTED rather
than raised: the method would have kept its in-process wrapper, lost
its rpc, and said so in a build warning nobody was reading.

It did not. `is_trusted_client` is in `_policy.py`, `async_store.py`,
`protocols.py` and `rpc.py`, and the protocol spells it `async def
is_trusted_client(self) -> TrustedFlag | None`.
