# Nothing can be built yet

**MOSTLY DONE.** `Store.build_paths` is bound, so the union finally
has the consumer it was built for. Two shapes beside it are not, and
each needs something this DSL does not have.

## What was missing

`tasks/059` and `tasks/063` built the whole `DerivedPath` machinery -
a real tagged union on the wire, a declared `Variant(...)`, a
generated caster - and the only thing consuming it was
`query_missing`, which says what building WOULD do.

`store.py` even named the gap in a comment: query_missing is "the
first consumer rather than `build_paths`" because it is read-only and
runs against a chroot store.

## Done

    def build_paths(self, targets: "list[DerivedPath]") -> None:
        Cxx("self.buildPaths(targets);")

One line, because the caster made the parameter `std::vector<
nix::DerivedPath>` already. The class is `blocking=True`, so the GIL
is released with no marker.

Two hermetic tests, and neither needs a builder:

- a path the store already holds builds as a NO-OP, twice, and the
  set of valid paths does not move;
- a well-formed ABSENT path raises, with libstore's own words -
  "no substituter that can build it". The test asserts the parse
  succeeded first, so it cannot pass because the wrong line threw.

## Still open

### A build MODE needs a C++ enum on the surface

Upstream takes `BuildMode`: `bmNormal`, `bmRepair`, `bmCheck`. This
binds the first and takes no parameter, which is narrower than the
C++ and never wider (CLAUDE.md goal 1).

`@words` does not fit. It is for a StrEnum whose member IS the string
libstore parses - `ContentAddressMethod` and `HashAlgorithm` both are,
and `pyenum.py` transforms the declaration into that module. `bmRepair`
is a C++ enumerator with no parser behind it, so a vocabulary would be
inventing the strings.

So this wants a marker for a real C++ enum: the enumerators, their
declared names, and how each crosses. `@tagged` already carries an
enumerator table for a union handle - `(reach, ask, {enumerator:
name})` - so the shape is not new, but its meaning is different and
folding them would make one marker mean two things (the same warning
`OutputsSpec` gave in tasks/063).

### buildPathsWithResults needs a BuildResult value

The other half of upstream's pair: a result per target, and no
exception on failure. That is the shape a caller wants when building
many things and reporting on each.

It needs `KeyedBuildResult` declared as a wire value, which is a
struct with a status enum, a message, timings and a map of built
outputs - so it waits on the enum question above.

### buildPathsWithResults is bigger than it looks

`BuildResult` is itself a SUM TYPE - `std::variant<Success, Failure>`
- where `Success` carries an enum and a `SingleDrvOutputs` map, and
`Failure` IS `BuildError`, an exception class with a second enum.
Two enums, a map and a variant whose arm is an exception.

So it is not "one more value type". It wants the enum marker above
plus a decision about how an arm that is an EXCEPTION crosses, which
is a question this repo has not been asked yet.

### And the enum marker has one ready user, not two

`BuildMode` is the only C++ enum a declaration is ready to name
today; `BuildResult`'s two are behind the paragraph above. CLAUDE.md
says one user is a helper and two is a pattern, so building the
marker now would be building it for one - and `tasks/063` already
recorded what that costs, when `OutputsSpec` nearly got folded into
a marker that meant something else.

The marker waits for its second user.

### evalStore

`buildPaths` takes an optional second store, used for derivations
only. Not bound, and not for a mechanical reason: what a Python
caller wants there is a design question rather than a parameter to
pass through.
