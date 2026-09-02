# An accessor's optional return reads a table, not the emitter

**OPEN, and it is a code reading rather than a measurement.** Nothing
declares the shape that hits it, which is why it has not fired.

`nbemit._accessor` gives a produced value's `Cxx`-bodied accessor an
explicit C++ return type, because a lambda with two return paths - a
value and `std::nullopt` - cannot deduce one. It gets that type from
a two-entry table:

    CXX_OPTIONAL = {
        "str | None": "std::optional<std::string>",
        "int | None": "std::optional<std::int64_t>",
    }

    spelled = CXX_OPTIONAL.get(m.ret.python if m.ret else "", "")

So the type is looked up by the literal annotation STRING. Any other
optional return gets no explicit type and the lambda does not
compile.

`_cxx` already answers the same question for every type the emitter
knows, and better: it resolves a vocabulary to `std::string`, a bound
class to its C++ name, a container to a vector or a map, and wraps
the lot in `std::optional`. A METHOD's return goes through it, which
is why `Store.is_trusted_client` returning `TrustedFlag | None`
emitted `-> std::optional<std::string>` with no table entry
(`tasks/070`). Only the accessor path reads the table.

## What would hit it

A produced value with a computed accessor returning:

- a VOCABULARY, `HashAlgorithm | None`;
- a bound CLASS, `StorePath | None`;
- anything with a width, `U64 | None` - and note the table's
  `"int | None"` is `int64_t`, so a `U64 | None` would silently get
  the SIGNED spelling if the annotation happened to read `int |
  None`.

`KeyedBuildResult` came within one field of this: `success()` returns
`BuildSuccess | None` and is written as a `Cxx` body. It compiles
because that emitter path is `_from_parts`-adjacent rather than
`_accessor`... which is worth checking rather than believing, because
this file is a code reading.

## What to do

1. **Confirm it, by breaking it.** Add a computed accessor returning
   `HashAlgorithm | None` to a produced value and watch the compile
   fail. If it compiles, this task is wrong and that is the finding.
2. Replace the lookup with `_cxx(m.ret, known)`, which is the same
   answer derived rather than tabulated.
3. Delete `CXX_OPTIONAL`. Check first that `_cxx` gives the same
   spelling for both entries: `"str | None"` matches, and `"int |
   None"` needs a declaration carrying `I64`/`U64` rather than a bare
   `int`, so a bare one may need refusing rather than defaulting to
   signed.

## Why it is worth doing

Goal 3. A rule applied identically belongs in the emitter, and this
one is applied in two places with two mechanisms - one derives, one
looks up a two-row table. They agree today because the table has the
two rows that came up first.

Opened 2026-09-02 while landing `TrustedFlag`, whose optional return
was expected to hit this and did not.
