# Should the protocol dicts be TypedDicts?

Raised 2026-08-25 while annotating the generator for 013.

## The state

The protocol dict is the central data structure of this repo. The pxd
and reflection produce it, every emitter reads it, the schema builder
reads it, the manifest IS it, and the server and client read that back.
It is currently `dict[str, Any]`, named `Proto` in each module that
handles one so the eventual decision lands in one place per module
rather than eighty.

`Any` satisfies a typechecker without telling it anything. A misspelled
key is a KeyError at build time, which is fine, but a key read with the
wrong SHAPE - a list where a dict was meant - is not caught anywhere.

## Why it is not obviously a win

A TypedDict wants a fixed set of keys. This one does not have that: a
proto gains keys as it moves down the pipeline, and which keys it has
depends on what it is.

    always      name module doc binds threading wire wire_fields
                blocking wrapped ctor methods bases
    wrappers    acquire (only when wrapped and constructible)
    wrapped     service protocol async_class rpc_class
    subclasses  async_base inherited
    values      message
    methods     rpc, protocol_blockers
    functions   wire_blockers

Modelling that honestly needs either `total=False` on most of it -
which makes every read Optional and buys little - or a union of several
TypedDicts with narrowing at each stage, which is a real refactor of
how the pipeline is written rather than a change of annotation.

The second is arguably the better design: the stages ARE distinct, and
naming them would document the pipeline. It is also the kind of change
that wants doing on purpose, not as a side effect of a lint pass.

## What would decide it

A bug that a TypedDict would have caught. There has not been one yet:
every protocol-dict bug this repo has hit was a key that was absent
(caught immediately) or a value that was wrong (a TypedDict would not
have helped). Worth revisiting when the manifest grows a second
consumer that is not in this repo - at which point the shape is a
published contract and not just an internal one.

## Cheaper thing first

The manifest already carries `"schema": 1` and nothing checks it. A
consumer reading a manifest from a different generator version has no
way to find out. That is a smaller job with a clearer payoff, and it
belongs with 022.
