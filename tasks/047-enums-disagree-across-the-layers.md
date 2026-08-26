# The three layers disagree about where an enum may appear

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

A string enum (038) is handled in three places, and they answer
differently for the same declaration:

- **Schema**: `wire_blocker` and `_msg_arg_type` accept an enum
  anywhere a scalar goes - alone, in `list[T]`, in `dict[str, T]`.
- **Codec, singular**: `WireCodec.encode` routes a scalar through
  `self.scalar()`, which knows enums. Fine.
- **Codec, in a container**: four sites, one cause - `_SCALARS`
  indexed where `self.scalar()` is meant (cython-worker confirmed
  all four against the code). Two crash: `list_to_msg` (wire.py:208)
  and `map_to_msg` (wire.py:178) raise KeyError on the first call
  that encodes a `list[HashAlgorithm]` or `dict[str,
  HashAlgorithm]`, both of which pass the build and get a schema.
  Two lie: `list_from_msg` returns `list(field)` and `map_from_msg`
  returns `dict(msg)` with no cast at all, so a decoded enum
  container arrives as bare strs - silently breaking the "a value
  read off the wire comes back TYPED" promise from 038. A bare enum
  parameter or return is fine: `encode`/`decode` already go through
  `self.scalar()`.
- **Wire fields**: `check_wire_contract` checks a field type
  against `_PRIMITIVES.values()` and the class protos. Enums are in
  neither set, so a `_wire_fields` entry of enum type fails the
  build as "unknown field type" - refused where the rpc layer
  accepts it.

Nothing declares an enum in a container or a wire field today, so
all of this is latent. That is exactly when it is cheap to fix.

## Fix sketch

One rule, held everywhere: an enum is a scalar. Then:

- All four container sites cast through `self.scalar(...)`, so
  containers encode and come back typed like singular fields do.
  The fix is a one-liner per site; the test carries the weight,
  because no binding declares an enum container today and the case
  has to be constructed.
- `check_wire_contract` takes the enum names into its known set, so
  a wire field may declare one.
- A perturbation test: declare `list[HashAlgorithm]` somewhere,
  round-trip it, remove it.

Alternative: refuse enums in containers and wire fields at build
time, consistently. That is smaller, but it refuses something the
schema can already spell, and a real Nix signature will want a list
of hash algorithms eventually.
