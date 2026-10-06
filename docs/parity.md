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
| Store operations | done | `AsyncStore`, `RPCStore`; same libstore calls |
| Evaluation | done | `AsyncEvalState`; `eval_expr`, `eval_file`, flakes, repl |
| Value accessors | done | Finer-grained than nanopynix (`integer`, `at`, `formals`); legitimate difference |
| REPL | done | `AsyncRepl`, `RPCRepl` |
| Flake lock + metadata | done | `AsyncLockedFlake`, `get_flake`, `call_flake` |
| Registry | done | Free functions + `decl/registry.py`; different spelling, legitimate difference |
| GC roots + collection | done | `add_temp_root`, `add_perm_root`, `find_roots`, `collect_garbage` |
| dump-db registration text | done | `make_validity_registration` |
| Primop registration primitives | done | `register_primop`, `make_primop`; the YAML set is policy, see below |
| Sync API | done | huggorm-only; nanopynix never had it |
| Remote + leases + detach | done | `NixClient`, `lifecycle.py`; richer than nanopynix rpc. A claimed state answers from its `fileEvalCache`, which a fresh state cannot |
| Warm eval + watch + notify | done | huggorm-only; `Watcher`, `Warmer`, `Notifier` |
| Session scope (local async) | done | `AsyncSession` + `AsyncSessionLike`, held to the generated ctors |
| Session scope (remote) | done | `AsyncRemoteSession`: typed acquires, token detach/claim, `share`, `attach`, sweep-aware close |
| Log bus + capture + verbosity | done | Sessions stream (`logs`, `process_logs`) and collect (`capture`) over per-state taps. Thread and default levels are settable; `nix::verbosity` is pinned at import from `HUGGORM_LOG_CEILING`, because every thread reads it. Legitimate difference |
| Dev-shell derivation rewrite | done | `huggorm.devshell` over generated ops, local sync+async; `read_derivation` and `to_json` cross RPC, the build stays where the environment is sourced |
| One consumer over sync, async and RPC | done | Every proxy has a service, and a proxy parameter is protocol-typed on every surface. `test_parity` runs eval, force, drv_path and the dev-shell rewrite as one body on all three, and `awrite_dev_shell_derivation` takes any `StoreLike` |
| print-dev-env (build + read + render) | done | `get_build_environment`/`print_dev_env` (+async): build every output, parse the dumped JSON into `BuildEnvironment`, render sourcable shell or `to_dict` JSON; redirects need installables and stay CLI. A `live` test compares both renderings with the `nix` CLI of the same version, flat and structured |
| Python store implementations | gap | Blocked on huggorm#84 |
| Typed settings models | superseded | Pydantic costs startup (CLI, completion); plain `dict[str, str]` is the surface |
| Typed store URI models | superseded | Same cost as above; plain URI strings are the surface |
| YAML primop set | refused | Tooling policy; lives in a consumer, over the primitives above |
| Overlay namespace setup | refused | Environment setup; lives with the caller |
| store_exec binary runner | refused | Tooling policy; lives with the caller |
| pynix CLI + LSP | refused | Consumers; they port after library parity |
