# Every int crosses the wire signed

**OPEN.** `SCALARS` maps the wire type `int` to `sint64`, once, for
every field. A declaration that says `U64` and one that says `I64`
reach the same proto type, and half of a u64's range does not fit in
it.

## What was measured

`GCOptions.max_freed` is a `U64` whose upstream default is
`std::numeric_limits<uint64_t>::max()`. Sending it:

    ValueError: Value out of range: 18446744073709551615
    huggorm/wire.py:524

That is the DEFAULT options object - what a caller sends when they
want no limit - so the whole class could not cross an RPC
(`tasks/074`).

## Why it had not fired before

Nothing had ever put a value above 2**63 on the wire. `nar_size` and
`download_size` are u64s and a real NAR size is nowhere near the
limit; `registration_time` is an i64 Unix time. The width has been
academic since the wire existed.

`max_freed` is the first, and only because upstream uses the largest
u64 as a SENTINEL rather than as a size.

## What 074 did instead, and why it is not this

The accessor answers None for the sentinel, so the absence crosses
rather than the number. That is right on its own terms - "no limit" is
not a size, and `PathInfo.registration_time` already reads upstream's
0-means-unknown the same way - and it happens to sidestep the width.

It fixes one field. The next u64 that legitimately exceeds 2**63 has
no sentinel to hide behind.

## Where the width is lost

Three places state a field's type and only the first knows the width:

1. the DECLARATION, which says `U64` or `I64` through an Annotated
   alias carrying a C++ spelling;
2. `_wire_fields`, emitted as `("max_freed", "int")` - the width is
   already gone here;
3. `SCALARS` in `pygen/grpc_schema.py`, which maps `"int"` to one
   proto type.

So the fix is not a bigger table. It is carrying the width from 1 into
2, and that changes what every int field emits - which makes it a
schema change, and puts it beside `tasks/022`.

## What to do

Decide first whether the wire should have two int types or one.

- TWO (`sint64` and `uint64`) is honest and costs a manifest change
  plus a schema change for every existing int field.
- ONE, if it is `sint64`, means a declaration may not say `U64` for a
  value that can exceed 2**63 - and the emitter should REFUSE one
  rather than let it fail at run time, which is the cheap half of this
  task and worth doing whichever way the rest goes.

Prove it by breaking it either way: a declaration carrying a u64 above
the signed limit must fail the BUILD, not a call.

Opened 2026-09-02, while closing `tasks/074`.
