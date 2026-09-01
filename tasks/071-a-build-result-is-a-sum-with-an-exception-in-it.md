# A build result is a sum with an exception in it

**OPEN, and it needs a decision before code.** `buildPathsWithResults`
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

  **DECIDED, not yet done.** Carl: Python's own time representation,
  which is `datetime.timedelta`. So the DSL wants a `Duration` alias
  the way it has `Path`, and nanobind ships the caster -
  `<nanobind/stl/chrono.h>` maps a `std::chrono::duration` to a
  `datetime.timedelta` both ways. What is NOT settled is what the
  WIRE carries: a timedelta is not a protobuf scalar, so it is either
  a well-known `Duration` message or an int of microseconds with the
  Python type rebuilt on arrival.

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

All of it. Nothing here is written; upstream was read on 2026-09-01
and the three answers above are the question, not a plan.
