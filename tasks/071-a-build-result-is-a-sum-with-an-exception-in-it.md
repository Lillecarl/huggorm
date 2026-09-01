# A build result is a sum with an exception in it

**OPEN, and it is now unblocked.** The decision below is made. `buildPathsWithResults`
is the last shape of `Store.build_paths` that is not bound, and it
cannot be declared the way the other returns were. The reason is one
line of upstream:

    using Failure = BuildError;

## What upstream has

`nix/store/store-api.hh:665`

    virtual std::vector<KeyedBuildResult> buildPathsWithResults(
        const std::vector<DerivedPath> & paths,
        BuildMode buildMode = bmNormal,
        std::shared_ptr<Store> evalStore = nullptr);

and upstream's own doc comment says what makes it different from
`buildPaths`:

    Note that in case of a build/substitution error, this function
    won't throw an exception, but return a BuildResult containing an
    error message.

`nix/store/build-result.hh`

    struct BuildResult {
        struct Success {
            using Status = enum BuildResultSuccessStatus;
            Status status;
            SingleDrvOutputs builtOutputs;
        };
        using Failure = BuildError;
        std::variant<Success, Failure> inner = Failure{};

        unsigned int timesBuilt = 0;
        time_t startTime = 0, stopTime = 0;
        std::optional<std::chrono::microseconds> cpuUser, cpuSystem;
    };

    struct KeyedBuildResult : BuildResult { DerivedPath path; };

    struct BuildError : public CloneableError<BuildError, Error> {
        Status status = MiscFailure;
        bool isNonDeterministic = false;
    };

So a result is a SUM, and one arm of that sum is an exception class -
`BuildError` inherits `Error` and is throwable. Upstream even keeps
`tryThrowBuildError()` on the struct, which turns the value back into
the raise that `buildPaths` would have done.

The two enums are the easy half. `BuildResultSuccessStatus` has four
words and `BuildResultFailureStatus` has twelve, and `tasks/070`'s
marker handles both without learning anything new - upstream has no
parser for either, so they take the `from_word` shape `BuildMode`
took.

## The decision

**How does a FAILED result reach Python?**

Three answers, and they are different APIs rather than different
spellings of one.

1. **As a value, with a status word.** `result.status()` answers
   `"permanent-failure"` and `result.message()` carries the text.
   Closest to what the C++ hands back, and the reason the method
   exists at all: a caller building twenty targets wants nineteen
   results and one failure, not one exception.

   Costs: the failure arm's `Error` half is thrown away, so a caller
   who wants the typed error the SYNC surface gives cannot have it
   here. And 059 declared a sum type as an ALIAS of its arms; this
   sum's arms are not two declared classes, they are a struct and an
   exception.

2. **As the exception, raised.** Which is `buildPaths`, and makes
   `buildPathsWithResults` pointless.

3. **As a value that CARRIES the exception.** `result.error()`
   answers a `NixError` subclass - the one `tasks/036` already gets
   across the wire with its class and its colour - and
   `result.raise()` mirrors upstream's `tryThrowBuildError`.

   This is the only one that loses nothing. It costs a declaration
   that says an error class is a FIELD of a value, which nothing
   declares today: 036 made an error CROSS as an error, and this
   would make one be a part of something else.

Answer 3 looks right and it is the most work, so it is a decision
rather than a default.

### DECIDED, 2026-09-01: it does not raise

Carl: "buildresult doesn't raise in Nix so it shouldn't raise in
Python either."

That rules out answer 2 outright, and it settles the SEMANTICS rather
than the shape: reading a failed result is never an exception. A
caller building twenty targets gets twenty results.

Answer 3 is what that leaves, and it does not conflict. Nothing about
it raises on its own - `error()` ANSWERS with the typed error rather
than throwing it, and that is the whole difference from `build_paths`.
Upstream's `tryThrowBuildError` is a method a caller may call, not
something the value does; binding it is binding what Nix has, and it
is the caller who chooses. If even that is too close to raising, the
method goes and `error()` stays - the value semantics are the part
that was decided.

Answer 1 stays rejected for the reason already written: it throws
away the `Error` half, so this surface would tell a caller LESS than
the sync one does about the same failure.

## What else was unsettled, and two of the three are decided

- `KeyedBuildResult` adds `path`, a `DerivedPath` - which is the
  union `tasks/063` and `tasks/067` already made cross. So the key
  half is free.
- `Success::builtOutputs` is `SingleDrvOutputs`, which upstream
  declares as `std::map<OutputName, Realisation>`.

  **DECIDED, and DONE.** Carl: the DSL should learn more complicated
  maps. It has: `dict[str, T]` over a declared class now spells
  `std::map<std::string, T>`, the same way `list[T]` already spelled
  a vector. So `built_outputs` needs nothing new when this is
  written.

  Two things went with it rather than beside it. The hard-coded
  `"dict[str, int]": nb::dict` table entry is deleted, because a body
  building an `nb::dict` by hand IS the mapping the emitter exists to
  derive - `gc_stats` returns the map now. And a container passes its
  alias's C++ spelling DOWN, which was a latent bug in the `list`
  branch too: `list[I64]` would have deduced a bare `int` with no
  width.

  Over RPC a map's values cross the way any value does. Carl again:
  a stateful unserializable object goes as a HANDLE - which is
  already what `wire_blocker` says, and it currently REFUSES a
  container of proxies outright ("one lease per element, and nothing
  grants leases in bulk", `tasks/031`). Bulk leases are that task,
  not this one.

- `cpuUser`/`cpuSystem` are `std::optional<std::chrono::microseconds>`.

  **DECIDED, not yet done.** Carl, twice: Python's own time
  representation - `datetime.timedelta` - and MICROSECONDS on the
  wire.

  So the DSL wants a `Duration` alias the way it has `Path`, and the
  two ends differ on purpose. nanobind ships the caster:
  `<nanobind/stl/chrono.h>` maps a `std::chrono::duration` to a
  `datetime.timedelta` both ways, so the in-process surface is a
  timedelta with no code of ours. The wire carries an int64 of
  microseconds and the codec rebuilds the timedelta on arrival,
  which is the same shape a vocabulary has: one Python type, a
  simpler thing on the wire, and a generated mapping at each end.

  Not a protobuf well-known `Duration` message, which was the
  alternative. It costs an import and a message where an int64 does,
  and `google.protobuf.Duration` splits into seconds plus nanos -
  a second representation to convert through for no gain over the
  unit upstream already uses.

- `timesBuilt`, `startTime`, `stopTime` are plain and need nothing.
  `startTime`/`stopTime` are `time_t`, which is a POINT in time
  rather than a span, so the timedelta answer does not cover them.

## Why it is worth doing

`build_paths` raises on the first failure and says nothing about what
succeeded. That is the wrong shape for the thing a caller most wants
to do with a store - build a list and find out what happened to each
element - and it is the only reason to bind this second method at
all.

## What is NOT done

All the CODE. Upstream was read on 2026-09-01, and the three
questions the task opened with are now answered: the map is built,
the duration is decided, and the result does not raise.

What is left to write, roughly in order:

1. **DONE.** The two status vocabularies. `BuildSuccessStatus` has
   four words and `BuildFailureStatus` has twelve, both with every
   enumerator named in `spelled` - upstream spells them CamelCase and
   a word is kebab-case, so the default spelling is right for none of
   the sixteen. Neither carries `parsed_by`, the same way `BuildMode`
   carries none: upstream parses neither from a string.

   The claim that a vocabulary emits nothing until something names it
   is MEASURED, not assumed. `bindings-src` after this commit has one
   occurrence of the string "BuildResult" in the whole tree, and it
   is inside `build_paths`'s docstring, which already said this task
   was coming. No `as_word`, no `from_word`, no include.

   What it DOES emit is the Python half: the StrEnum in
   `huggorm_bindings/words.py` and its stub entry. That found the
   thing `tasks/064` is about - both front doors carry hand-written
   re-export lines, so the stub gate failed with `__init__.pyi
   exports [...BuildFailureStatus...], the package exports [...]`
   until two names were added to each by hand. Four lines of a fact
   the emitter already knows.
2. the `Duration` alias, its `<nanobind/stl/chrono.h>` caster, and
   the microsecond form on the wire;
3. `BuildResult` itself, which is where the new DSL is: a value one
   of whose fields is a declared ERROR class. `tasks/036` made an
   error CROSS as an error; nothing yet makes one be PART of
   something else;
4. `Store.build_paths_with_results`.

### DECIDED, 2026-09-01: two vocabularies, not one merged list

The open shape question, which was a reading of upstream rather than
Carl's call. Upstream's own comment on both status enums says "Names
must be disjoint with" the other, which WOULD license one Python
vocabulary of sixteen words over two defaultless switches.

Declined, for two reasons found in the code.

`Enumerated` names ONE C++ enum, and both directions are keyed by it.
A merged list would have to answer which `from_word<T>` the word
"built" belongs to, and the only thing that knows is the arm the
result holds - which the caller already has. So the merge buys a
shorter word list and pays with an ambiguity the DSL has no way to
say.

And the arms carry different things. A success holds `builtOutputs`;
a failure holds a message and `isNonDeterministic`. A caller branches
on which arm it got whatever the word list looks like, so a single
`status` would let a caller THINK the branch was optional.

The shape that follows is upstream's own: `tryGetSuccess` and
`tryGetFailure` return pointers, so Python gets two optional arms
rather than one flat value with empty fields.

Declining the merge does not make the invariant stop mattering, so
`test_the_two_build_statuses_keep_upstream_disjoint` asserts it -
both the enumerator names and our sixteen words. PERTURBED: setting
`MISC_FAILURE = "built"` in the declaration fails it with `our words
collide: {'built'}`, so the gate is known to test something.
