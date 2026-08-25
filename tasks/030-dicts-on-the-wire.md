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
