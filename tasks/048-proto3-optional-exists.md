# The presence story rests on a proto3 limitation that was lifted

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

Several build refusals and one runtime conflation all cite the same
fact: "proto3 gives a scalar field no presence".

- `wire_blocker` refuses a `str | None` / `int | None` return
  outright, with that sentence as the reason.
- `WireCodec.decode` reads an optional scalar as
  `if optional and not raw: return None` - so a genuine 0, "" or
  False decodes as None. The docstring admits it.
- `check_wire_contract` lets a scalar `_wire_fields` entry carry
  `?`, which then hits the conflation above at runtime.

The cited fact is out of date. proto3 has had explicit `optional`
on scalar fields since protobuf 3.15 (2021): a synthetic oneof
gives the field real presence, `HasField` works on it, and every
implementation ships it. The schema here is built by hand as a
FileDescriptorProto, so emitting it means setting
`proto3_optional = True` and adding the synthetic oneof entry -
mechanical, in one place (`grpc_schema._field`).

Confirmed with cython-worker: the field case was on the radar
(an explicitly-passed "" reading back as None was raised twice
this session) and is unowned; the `wire_blocker` wording states a
limitation of proto3 where the true limitation is what this
generator emits. So there are two symptoms of one missing feature:
an optional scalar FIELD that is silently wrong, and an optional
scalar RETURN that is loudly refused. The field half is the one
that can be wrong rather than absent.

## Why it matters

`query_path_from_hash_part` returning `StorePath | None` worked
only because StorePath is a message. The first real method
returning an optional scalar - a build log that may not exist, an
optional narinfo field - gets refused today, and the workaround
would be a wrapper message, which is the schema knowing something
the binding could not say.

## Fix sketch

- Emit `proto3_optional` for a scalar field whose declared type is
  `T | None` or whose wire field carries `?`.
- Decode with `HasField` for those, exactly as message fields do
  now; delete the `not raw` heuristic.
- Lift the `wire_blocker` refusal for optional scalars and enums.
- The refusal for an optional CONTAINER stands: repeated fields
  genuinely have no presence, and "absent IS empty" is the right
  answer there (041).

Ordering: do this before or with 022. It adds oneof entries to
messages, which the field-number lockfile must record.
