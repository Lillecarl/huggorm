# What replacing nanopynix-bindings needs

**OPEN.** The board for one goal: nanopynix's Python layer runs on
`huggorm_bindings` instead of its hand-written `nanopynix_bindings`.
Measured 2026-09-25 against nanopynix `ce5ff758` and huggorm
`18480a8d`. The huggorm gate built green at that revision.

## Where nanopynix touches its bindings

One layer does: `nanopynix/src/nanopynix/_core/` (`NixCore`,
`CoreStore`, `CoreEvalState`, `CoreValue`, `CoreLockedFlake`, the
thread executor). The inproc engine and the rpc worker call `_core`,
and call the bindings directly only for process and thread globals:
the logger, verbosity, the request id, activity tracking and
evaluator-thread registration. The rpc client never calls them.
pynixd and nixkube do not import them.

So a replacement is a port of `_core` plus those globals, not of
nanopynix as a whole.

**The target is the capability, not nanopynix's names.** `GATES`
already calls that corpus one to beat. Where the table below names a
nanopynix spelling, read it as "a caller can do this". `_core` is
rewritten against huggorm's shapes, and huggorm does not copy its
module layout or its class names.

## The gap, by area

What nanopynix calls outside its own tests, and what huggorm has.
"None" means no declaration names it.

| Area | nanopynix uses | huggorm |
|---|---|---|
| Settings | `init_libstore(load_config)`, `set/get/list_settings`, `reset_overridden`, `enable_experimental_feature`, three `*_settings_metadata_json`, `build_info()` capabilities, `current_system()`; eval, fetch and flake settings registered on `GlobalConfig` so nix.conf reaches them | none |
| Cancellation | `InterruptToken`, `interrupt_scope` over the thread-local `interruptCheck`; `nix::Interrupted` raises `OperationCancelled` | none |
| Logger | one leaked `nix::Logger` calling a Python callback; per-thread verbosity; thread-local request id; `set_activity_tracking` filters build and copy activities in C++ | a queue tap (`subscribe_logs`, `drain`), `begin_request`/`end_request`, `process_verbosity`. A pull model, not a callback |
| Store | `close`, `get_store_dir(s)`, `get_uri(with_params)`, `get_build_log`, `read_derivation_typed` (a `Derivation` class), `write_dev_shell_derivation`, `dump_db`, `copy_closure`, `compute_store_path`, `find_roots`, `add_perm_root`, `add_indirect_root`, `optimise_store`, `verify_store`, `query_derivation_outputs`, `build_paths_with_results(build_mode, eval_store)`; `parse/render_store_reference`, `list_store_types_json` | about half: path info, closure, referrers, missing, build, GC, temp roots, substitutable. No derivation, no copy, no roots beyond temp, no build mode (069) |
| Eval | `EvalState(store, search_path, build_store, eval_settings, fetch_settings)`, `eval_string(expr, path)`, the REPL family (9 methods), `statistics_json`, `reset_file_cache`, `value_from_python`; `parse_nix_path`, `is_pseudo_url`, eval counters, evaluator-thread enter/exit | `EvalState(store)`, `eval_expr`, `eval_file`, `forget_file`, `register_primop` |
| Value | `to_python`, `to_json(copy_to_store)`, floats, `realise_string`, `realise_argv`, `edit_location`, `get_doc`, `attr_doc`, `call`, `auto_call`, `build`, `derived_path` | ints, strings, bools, lists, attrs, `apply`, `apply_auto`, lambda and primop introspection, `doc` |
| Primops | `register_primop(name, arity, arg_names, doc, cb)`, `PrimopError`, `__sleep` | `register_primop` (033) |
| Fetchers | `Input.to_attrs`, the registry (list, add, remove, pin, user path) | none |
| Flakes | `parse_flake_ref`, `lock_flake`, `get_flake`, `call_flake`, `eval_flake`, `metadata_json`, `LockedFlake` | none |
| Store impls | `register_store_implementation` (Python-backed `nix::Store`), `process_connection` | none. Needs virtuals, see 084 |
| Errors | 16 error kinds a caller tells apart, each with its rendered text and a position, trace and suggestions | a declared hierarchy (036). Not checked against those 16 kinds |
| Nix versions | every nixpkgs Nix from 2.34, plus git; feature detection where git reports `2.36pre` | one version, `pkgs.nix` (055, undecided) |

## The logger needs no design change

The queue is the right shape. Filtering in C++ keeps evaluation from
waiting on Python, and nanopynix's `--nom` work needed exactly that.
The tap already never drops a "stop" (`test_a_stop_is_never_dropped`).

It does drop a "result" at capacity (`logging.hpp:159`). A dropped
`resProgress` gives a build monitor wrong counts, and a dropped
`resSetPhase` a stale phase. What is missing is nanopynix's activity
filter: build and copy activities, and their results, delivered at
any verbosity and never dropped. That is a rule the tap can carry.

## Defect found while measuring: nix.conf never reached an evaluator

`EvalSettings` and `fetchers::Settings` were never registered on
`globalConfig`. `nix` registers them from libcmd, and huggorm does
not link libcmd. So `pure-eval = true` in nix.conf left
`builtins.currentTime` defined, where `nix eval` answers false, and
nothing warned. `cpp/settings.hpp` registers both and replays what
the file set onto each state. `tests/test_settings.py` failed on the
old bindings in the two cases that expect purity.

## Ranked by what blocks the most

1. Settings and init. The nix.conf defect above is fixed. Still
   missing: reading, setting and listing a setting, the metadata,
   and experimental features.
2. Cancellation. Every long call in `_core` runs under an interrupt
   scope.
3. `Derivation`, `get_build_log`, build mode and `copy_closure`.
   `pynix build` needs these.
4. Eval constructor arguments, `eval_string(path)` and the `Value`
   conversions. `pynix eval` needs these.
5. Flakes and fetchers.
6. REPL, Python store implementations, the daemon protocol.
7. The Nix version matrix (055).
