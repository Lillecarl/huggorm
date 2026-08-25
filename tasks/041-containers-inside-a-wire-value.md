# A wire value cannot hold a list

Found binding `query_path_info`. `PathInfo` carries what a store knows
about a path, and two of the fields Nix gives are sets:

    StorePathSet references;   // what this path points at
    std::set<Signature> sigs;  // who vouched for it

Neither is in the binding, and this is why.

## Where it stops

`_wire_fields` names a field's type, and the codec reads it in
`wire.value_to_msg` / `value_from_msg`. Both handle exactly two kinds:
a scalar, which is assigned, and another wire value, which is filled
in place. A `list[T]` field falls through to `setattr(msg, fname,
val)`, and a repeated protobuf field cannot be assigned.

The RPC layer already knows how to do this. `list_to_msg` and
`list_from_msg` exist and carry `query_all_valid_paths` - a repeated
field of nested messages, order kept. What is missing is that a wire
VALUE's own fields never route through them.

So the work is not new machinery. It is `value_to_msg` and
`value_from_msg` asking `kind()` for a third answer and calling the
list helpers that are already there, plus `grpc_schema` building the
value message's field as `LABEL_REPEATED` - which `_add_field`
also already does, for method parameters.

## Why it was left out rather than half-done

`references` is the field that makes a store path a graph rather than
a name, so it is worth having and worth having right. Adding it as a
`str` list of base names would have crossed the wire today and thrown
away the type; adding it as `list[StorePath]` needs the above.

`PathInfo` without it still answers the questions a caller asks first
- how big, what hash, built by what, when - so the binding lands and
this follows.

## The other half: a parameter

`add_to_store` and `add_path_to_store` both take `references` in C++
and both pin it to empty. That is the same type in the other
direction, and it has an extra problem the return does not: what does
it DEFAULT to?

`[]` is a mutable default argument, and every generated surface would
carry one. `None` is refused as a method default today, for a reason
that does not apply here - a repeated field has no presence problem,
because an absent one and an empty one mean the same thing. So a
container-typed parameter is the case where a None default is
representable, and `default_source` would need the parameter's TYPE to
know that.

Worth doing when a caller wants a reference, which is the moment a
path stops being standalone: a wrapper script pointing at a binary.

## Also missing, noticed here

`nix::Store::toStorePath(path)` answers "which store path contains
this file", which is a different question from `parse_store_path`.
`sys.executable` is inside a store path and is not one, and the live
test had to use `sys.prefix` to avoid the gap.
