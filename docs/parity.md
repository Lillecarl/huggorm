# Parity: nanopynix retires when the gap column is empty

The vision: huggorm replaces nanopynix entirely. Every binding fact
is generated from a declaration, so surfaces cannot drift. The
hand-written layer holds composition only, and a gate holds each
composition to the surface it wraps.

nanopynix stays until this file says otherwise. No shortcut.

## Statuses

- **done**: huggorm serves it. A different spelling is a legitimate
  difference, and the row says so.
- **wip**: hand-written composition in progress.
- **gap**: open work, with the issue that tracks it.
- **refused**: belongs to a consumer (pynix, an editor, a tool), not
  to this library.
- **superseded**: nanopynix had it, huggorm will not. The row gives
  the reason.

## Rows

| nanopynix capability | status | note |
|---|---|---|
| Low-level bindings | done | `huggorm_bindings`; nanopynix already runs on it |
| Store operations | done | `AsyncStore` (51), `RPCStore` (43); same libstore calls |
| Evaluation | done | `AsyncEvalState` (29); `eval_expr`, `eval_file`, flakes, repl |
| Value accessors | done | Finer-grained than nanopynix (`integer`, `at`, `formals`); legitimate difference |
| REPL | done | `AsyncRepl`, `RPCRepl` |
| Flake lock + metadata | done | `AsyncLockedFlake`, `get_flake`, `call_flake` |
| Registry | done | Free functions + `decl/registry.py`; different spelling, legitimate difference |
| GC roots + collection | done | `add_temp_root`, `add_perm_root`, `find_roots`, `collect_garbage` |
| dump-db registration text | done | `make_validity_registration` |
| Primop registration primitives | done | `register_primop`, `make_primop`; the YAML set is policy, see below |
| Sync API | done | huggorm-only; nanopynix never had it |
| Remote + leases + detach | done | `NixClient`, `lifecycle.py`; richer than nanopynix rpc |
| Warm eval + watch + notify | done | huggorm-only; `Watcher`, `Warmer`, `Notifier` |
| Session scope (local async) | done | `AsyncSession` + `AsyncSessionLike`, held to the generated ctors |
| Session scope (remote) | done | `AsyncRemoteSession`: typed acquires, token detach/claim, `share`, `attach`, sweep-aware close |
| Sync session scope | gap | No sync remote exists; sync local need unproven |
| Log bus + capture + verbosity | gap | Per-state `subscribe_logs` exists; session-scoped stream and capture do not |
| Dev-shell derivation rewrite | gap | `get-env.sh` already ships in `huggorm-bindings`; the rewrite is open |
| Python store implementations | gap | Blocked on huggorm#84 |
| Typed settings models | superseded | Pydantic costs startup (CLI, completion); plain `dict[str, str]` is the surface |
| Typed store URI models | superseded | Same cost as above; plain URI strings are the surface |
| YAML primop set | refused | Tooling policy; lives in a consumer, over the primitives above |
| Overlay namespace setup | refused | Environment setup; lives with the caller |
| store_exec binary runner | refused | Tooling policy; lives with the caller |
| pynix CLI + LSP | refused | Consumers; they port after library parity |
