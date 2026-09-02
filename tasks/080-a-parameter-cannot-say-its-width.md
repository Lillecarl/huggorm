# A parameter cannot say its width

**OPEN.** `params[].type` is one string with two readers. The stub
emitter reads it as a Python annotation and the schema builder reads
it as a wire type. A width is right for the second and wrong for the
first, so the manifest carries neither and the build refuses the
declaration instead.

## What refuses today

`manifest._crossable`, on a proxy's methods and on the parameters
that acquire one, and on a wrapped free function. A `uint64_t` there
fails the build with a message naming this file.

Nothing declares one. Every integer a service carries is an `I64`:
`Value.at(index)`, `Value.integer`, `Value.size`,
`EvalState.make_int(value)`. So
the refusal is a guard over an empty set, and it exists because the
alternative was a number truncated in silence (`tasks/079`).

## Why the obvious fix is not obviously right

A second key beside `type` - `"wire": "uint"` - is what a field
already has, and `_wire_fields` proves the shape works.

`model.py` is the problem. It reflects the same manifest off a
COMPILED class, and `check.py` diffs the two. A key reflection cannot
produce is a diff that never closes. So this is not one emitter
change; it is a change to what both sides of that diff mean by a
parameter.

## What to decide first

Whether the manifest should carry the Python annotation and the wire
type as two keys everywhere, or whether the wire type belongs
somewhere else entirely - the schema builder already reads `kinds`
beside the manifest, and a width is the same kind of fact.

`tasks/022` is the other half of this. It is a manifest SHAPE change
either way, and the two should be decided together.

## How to prove it

Give a proxy a method taking a `U64` and send `2**63 + 1` through it.
Today that fails the build. It should reach the far side unchanged,
and the stub for it must still say `int`.

Opened 2026-09-02, while closing `tasks/079`.
