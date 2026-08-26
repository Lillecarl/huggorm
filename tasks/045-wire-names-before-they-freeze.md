# Wire names still say "nixmock", and freeze soon

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

The wire carries names from the mock era, and one naming
inconsistency of its own:

- `grpc_schema.PKG = "nixmock.v1"`. Every service, message and rpc
  path starts with `nixmock`, and real Nix types now cross under it.
- `lifecycle.TOKEN_HEADER = "x-nixmock-conn"`.
- Thread name prefixes are `flg-pool` / `flg-affine-*`
  (runtime.py, emitter.py). `flg` names nothing in this repo.
- `req_name` keeps the method's snake_case
  (`Store_add_to_storeReq`) while `resp_name` title-cases it
  (`Store_AddToStoreResp`). Two spellings for one method, in one
  schema.

Confirmed with cython-worker: all three are leftovers with no
deliberation behind them (`flg` is "fake-library generated"). A
rename is safe today ONLY because the manifest, the schema and both
peers come out of one generator run; it stops being safe the moment
an out-of-tree client exists.

## Why now

Task 022 wants a lockfile that pins (message, field) pairs so
external consumers survive rebuilds. A rename after that lock exists
is a breaking schema change with ceremony. A rename before it is a
constant edit. The same argument covers every external consumer 022
lists: grpcurl scripts, protoc clients, a persisted evaluation
server whose clients were built last week.

## Fix sketch

- Pick the real package name once (`cythonix.v1` is the obvious
  candidate) and change `PKG`, `FILE` and `TOKEN_HEADER` in the
  same commit. The naming already lives in one module each
  (grpc_schema.py, lifecycle.py), so the edit is small - which is
  the design working as intended.
- Make `req_name` and `resp_name` agree on one casing.
- Rename the `flg-` thread prefixes to `cythonix-`. Tests that
  match thread names by prefix must move with them.

Alternative rejected: leave it until 022 lands. The lock would then
record the wrong names, and the migration doubles.
