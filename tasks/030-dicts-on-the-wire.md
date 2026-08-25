# Dicts can cross the wire: Nix attribute keys are strings

Carl, 2026-08-25: "All dict keys in Nix are strings."

## Why that matters

`gc_stats` is the only binding function with no RPC surface, and the
build says why:

    warning: gc_stats has no RPC surface - return type: dict has no
    wire representation; the schema has no map or struct type yet

That blocker was written when the answer was unknown. It is known now.
An attribute set has string keys, so `map<string, V>` covers every dict
this API will ever return, and protobuf has had map fields since
proto3's first release.

It is not only gc_stats. An attribute set is the shape Nix evaluation
hands back for most things worth asking for, so an evaluation server
(016) that cannot put one on the wire is missing its main return type.

## What it takes

- `_msg_arg_type` learns a map case, keyed by string, valued by
  whatever the declared value type is.
- The value type has to come from somewhere. `dict` alone says
  nothing, so the binding declares `dict[str, int]` the way gc_stats
  now does for the typechecker's benefit - which means the same
  annotation feeds both, and `_annotation_name` has to stop flattening
  a subscripted generic to its head.
- `wire_blocker` keeps reporting an unparameterised `dict`, because
  that one genuinely cannot cross: the schema needs a value type.
- The codec grows an encode/decode pair for maps, recursing into the
  value type the same way `value_to_msg` already recurses into a
  nested wire-value.

## Watch out for

A map value that is itself a proxy. `map<string, Handle>` is
representable, but the lifetime rules for a handle arriving inside a
collection are not written down anywhere - each one is a lease, and
nothing currently grants leases in bulk. Start with scalar values and
say so.

Nested dicts. `map<string, X>` cannot hold another map directly in
proto3; it needs a wrapper message.

## Decided 2026-08-25

Carl: a recursive Value message is the correct way to do it, not a
map of maps.

That settles the shape and makes the flat-map version a stepping
stone rather than the destination:

    message NixValue {
      oneof v {
        string  s     = 1;
        sint64  i     = 2;
        bool    b     = 3;
        double  f     = 4;
        Handle  proxy = 5;   // a Value that stays remote
        NixList list  = 6;
        NixAttrs attrs = 7;  // map<string, NixValue>
      }
    }

Which changes the order of work. A recursive message carrying a Handle
in one of its arms means proxies appear at arbitrary depth, so 031
(recursive handle tracking, and the identity-mapping that has to
precede it) is a prerequisite rather than a parallel concern.

It also means the `proxy` arm is where laziness lives: a thunk cannot
be serialized, so an unforced value crosses as a handle and the client
forces it with another call. That is the same value/proxy split the
whole design already rests on, one level deeper - and it is why this
message cannot be generated from a `_wire_fields` declaration the way
StorePath's is. It is recursive, and its arms are the wire kinds
themselves.

## Done 2026-08-25: the flat map

`dict[str, V]` crosses as `map<string, V>`. `gc_stats` was the last
function with no RPC surface and now has one, proven end to end and
through grpcurl - which only has the descriptor this build emitted, so
a wrong synthesised entry message fails there even when it parses in
Python.

What it took, against the sketch above:

- `_annotation_name` needed no change. It already renders a
  subscripted generic in full, so `dict[str, int]` reached the
  manifest intact and the emitter, which parses annotations with
  `ast`, already wrote it into the async form and the stub.
- `_msg_arg_type` was the wrong place. proto3 spells a map as a
  repeated field of a message the containing type carries, so the
  builder has to CREATE something, not classify. `_add_field` does
  that and every field now goes through it.
- `wire_blocker` reports what a map cannot do in its own words: a bare
  `dict`, a non-string key, a map of maps, a map of proxies.
- The codec grew `map_to_msg` / `map_from_msg` beside the wire-value
  pair they mirror.

New: `codegen/wiretypes.py`, copied into the package as
`_wiretypes.py` the way `_runtime.py` is. Reading `dict[str, int]` as
a map is the one piece of knowledge the manifest cannot carry, because
it is about how an annotation is SPELLED rather than about the types.
The schema builder needs it at build time and the codec at run time,
so they read one definition instead of two.

Two things stay blocked on purpose, both waiting on the recursive
value message:

- a map of maps. proto3 will not synthesise the wrapper.
- a map of proxies. Every entry would be a lease, and nothing grants
  leases in bulk (tasks/031).

## Done 2026-08-25: the mock has collections

A value can now be a list or an attribute set holding other values, so
a value is a tree and any node of it may still be a thunk. Built, not
parsed (Carl: "just builder methods, we don't want to reimplement
Nix"). Attributes live in a sorted array like nix::Bindings, because
Carl confirmed Nix attribute sets are alphabetical - so an index walk
IS the listing order and `dict[str, V]` is the right Python shape.

The binding surface is index-based and container-free:
size/at/name_at/value_at/has/get to read, make_list plus list_append
and make_attrs plus attrs_set to build. That is not a style choice.
The pxd declares this API to Cython, and a template type there maps to
nothing, so a declared surface is scalars and Value pointers.

Two things fell out of it:

- the pxd parser answered "void" for any type it could not render, so
  a `vector[string]` return would have become None with nothing
  reporting it. Fixed and gated.
- a RETURNED type producing another returned type was not adopted into
  its async wrapper. A Value that holds Values is the first thing with
  that shape, and the conformance gate named all three methods.

## What is left: the recursive message

`Value.attrs() -> dict[str, Value]` and `Value.items() -> list[Value]`
are deliberately absent. They need a collection of PROXIES, which is
the NixValue message, and that message needs a decision this file
cannot make on its own:

**What does a client ask for?** The message shape is settled. The rpc
that carries it is not. A tree that forces everything can be unbounded
and can raise halfway down; a tree that forces nothing is a handle the
client already had. Somewhere between them is a `Realize(handle,
depth)` that serializes what is forced, leaves a thunk as a proxy, and
stops at a depth the caller names.

And every proxy in that tree takes a lease. A realize over a large
attribute set hands the client hundreds of handles to release. The
client's finalizers do balance it (tasks/028), but the round trip that
saved N calls costs N handles, which may argue for the depth limit
being small by default.
