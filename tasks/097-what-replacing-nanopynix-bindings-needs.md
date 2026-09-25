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
| Store | `close`, `get_store_dir(s)`, `get_uri(with_params)`, `get_build_log`, `read_derivation_typed` (a `Derivation` class), `write_dev_shell_derivation`, `dump_db`, `copy_closure`, `compute_store_path`, `find_roots`, `add_perm_root`, `add_indirect_root`, `optimise_store`, `verify_store`, `query_derivation_outputs`, `build_paths_with_results(build_mode, eval_store)`; `parse/render_store_reference`, `list_store_types_json` | about half: path info, closure, referrers, missing, build with a mode, GC, temp roots, substitutable, `copy_closure`, `get_build_log`. No derivation, no roots beyond temp, no `eval_store` |
| Eval | `EvalState(store, search_path, build_store, eval_settings, fetch_settings)`, `eval_string(expr, path)`, the REPL family (9 methods), `statistics_json`, `reset_file_cache`, `value_from_python`; `parse_nix_path`, `is_pseudo_url`, eval counters, evaluator-thread enter/exit | `EvalState(store)`, `eval_expr`, `eval_file`, `forget_file`, `register_primop` |
| Value | `to_python`, `to_json(copy_to_store)`, floats, `realise_string`, `realise_argv`, `edit_location`, `get_doc`, `attr_doc`, `call`, `auto_call`, `build`, `derived_path` | ints, floats, strings, bools, lists, attrs, `apply`, `apply_auto`, lambda and primop introspection, `doc`, `to_json`, `drv_path`. The realize tree is the `to_python` |
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
nothing warned. `NIX_PATH` was lost the same way: `initGC` copies it
into the `nix-path` setting through `globalConfig`, so `<nixpkgs>`
never resolved from the environment. `cpp/settings.hpp` registers both and replays what
the file set onto each state. `tests/test_settings.py` failed on the
old bindings in the two cases that expect purity.

## Cancellation poisons the thunk it interrupts

Measured 2026-09-25, with nanopynix's own bindings, because huggorm
has no interrupt yet (`.scratchpad/poison_probe.py`):

    force root.a under a scope cancelled at 0.2s  -> OperationCancelled, 0.34s
    force root.a again, no scope                  -> KeyboardInterrupt:
                                                     "interrupted by the user", 0.00s

`EvalState::handleEvalExceptionForThunk` (`eval.cc:2188`) stores any
exception in the thunk with `mkFailed`. Only a `RecoverableEvalError`
keeps a recovery thunk, and `nix::Interrupted` derives `BaseError`,
so an interrupted thunk rethrows the interruption on every later
force. `nix` never sees this, because an interrupt ends its process.
A warm state that outlives a cancelled call does see it. nanopynix
ships it today, and reports the rethrow as `KeyboardInterrupt`
because no token is armed any more.

Upstream fixed it in 2.35.0 (5c4f498d3, NixOS/nix#15980), in the
same shape: a recovery thunk for `Interrupted`. huggorm binds 2.34.8
and carries that commit as `nix/patches/nix-interrupted-thunk-
recovers.patch`. nanopynix's 2.35 and git lanes have it already; its
2.34 lane does not (nanopynix#309).

`nix::Interrupted` also derives `BaseError` and not `Error`, so a
catch chain rooted at `nix::Error` misses it and nanobind raises a
bare `RuntimeError`.

## Ranked by what blocks the most

1. Settings and init. The nix.conf defect above is fixed, and
   `get_setting`, `set_setting`, `list_settings(overridden_only)`
   and `settings_json` are declared in `decl/eval.py`. A feature is
   enabled with `set_setting("extra-experimental-features", ...)`,
   because `Config::set` takes nix.conf's `extra-` prefix. No
   function of its own. None of them has an rpc: they change the
   process, and a remote client changing a shared service's
   configuration is a decision nobody has made. `EvalState(store_uri,
   settings)` takes one state's own evaluator and fetcher settings
   over the process's. A store setting there raises, because the
   state has none of its own. Adding it found an emitter defect: a
   constructor default without a C++ body never reached the binding.
   Still missing: `current_system`.
2. Cancellation. Done in process: `begin_request` installs a hook on
   `nix::unix::interruptCheck`, `cancel_request` marks a request, and
   the runtime cancels the request when its await is cancelled, then
   waits, shielded, until the thread has stopped. `Interrupted` is a
   `BaseException`. The rpc side is `tasks/098`.
   A deadline is NOT honoured for work that never reaches a
   `checkInterrupt`, such as a long fetch: the shielded wait lasts
   until the call ends. The alternative not taken is nanopynix's: a
   grace period, then an abandoned ("poisoned") executor that refuses
   later calls. Waiting keeps the state usable; abandoning keeps the
   deadline.
3. `Derivation`, `get_build_log`, build mode and `copy_closure`.
   `pynix build` needs these. Build mode was already there (069's
   table above was stale). `get_build_log` and `copy_closure` are
   declared. `copy_closure` needed a proxy parameter the call writes
   to, so a proxy now crosses into C++ as `T &` and only a wire value
   as `const T &`. `Value.drv_path()` joins evaluation to building:
   it answers the `.drv` of a derivation value, through libexpr's
   `getDerivation`, and `DerivedPathBuilt` takes it. `pynix build`
   needs that join more than it needs `Derivation`, which is for
   `pynix develop`. Still missing: `Derivation`, and `eval_store` on
   `build_paths`.
4. Eval constructor arguments, `eval_string(path)` and the `Value`
   conversions. `pynix eval` needs these. Floats are done: the DSL
   has `F64` (a C++ double, a proto double), and a float crosses a
   realized tree as itself. The search path needs no parameter:
   `nix-path` and `extra-nix-path` are evaluator settings, so
   `EvalState(uri, {"nix-path": ...})` sets it per state. One
   difference from `-I`: `-I` entries come before `nix-path`. Still
   missing: nothing in this item. `EvalState(uri, settings,
   build_store_uri)` splits evaluating from building, as `nix
   --eval-store` does; no gate builds in it, because the sandbox
   cannot run a builder. `eval_expr(expr, base)`,
   `to_json`, `realise_string` and `realise_argv` are done. The two
   realises pass `isIFD = false`, where nanopynix passes true: a
   caller realising a value it holds is not an import during
   evaluation, so `allow-import-from-derivation = false` must not
   refuse it.
5. Flakes and fetchers.
6. REPL, Python store implementations, the daemon protocol.
7. The Nix version matrix (055).
