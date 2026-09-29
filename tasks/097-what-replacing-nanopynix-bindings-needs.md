# What replacing nanopynix-bindings needs

## The drop, 2026-09-29

Carl: "drop it and remove any compatibility shit". Asked how far, he
chose to dissolve the adapter too, not only delete the bindings.

**Stage 1, in nanopynix's working copy (not landed).** Every scope
builds on `huggorm-bindings` (sanitizers through `overrideAttrs`, a
no-GC scope passes no libgc), the wheel is gone for good (Carl: not
worth the effort; nanopynix#311 closed), `_engine.py` imports the adapter alone, the `nix_engine`
marker and the bindings-only tests are deleted, and the workflows are
re-rendered. It cannot land alone: `checks.types` reports 902 errors,
because pyright typed `_core` through the bindings' stubs and the
adapter's namespaces are untyped. 763 of them are in `_core/_objects.py`,
`inproc/_impl.py` and `_core/_nix_core.py`: the adapter's callers.

**Stage 2 is what makes it land.** `_engine.py` re-exports huggorm's
real names, which pyright types through `huggorm_bindings-stubs`, and
callers move onto them one area at a time, deleting the adapter code
each area orphans:

1. `_core/_objects.py` and `_core/_nix_core.py`: Value and EvalState.
2. `inproc/_impl.py` and `rpc/worker/_worker_store.py`: Store.
3. The remaining areas: flake, fetchers, registry, logging, settings.

Lane part A after each area; one land at the end, with easykubenix's
`nanopynix-bindings` passthru changed in the same land.

Carl's calls on the features the adapter could not serve:

- `register_store_implementation` and `nanopynix.store_impl`: PORT.
  Needs a declared Store subclass that calls into Python, which is
  `tasks/084`'s gap (a declaration cannot implement a virtual). The
  deleted `test_store_backend_registration.py` is the target to bring
  back.
- `eval_counters_enabled` / `set_eval_counters_enabled`: PORT. huggorm
  declares them over the `count-calls` option nanopynix's patch adds,
  so huggorm's own Nix needs that patch per version.
- `process_connection`: DROP.

**Stage 2, done in nanopynix's working copy (not landed).** Six commits
on top of stage 1: the evaluator, the store, the adapter's deletion, the
`process_connection` drop, and two comment sweeps. `_engine_huggorm.py`
is gone, `_engine.py` is again the one importer of `huggorm_bindings`,
and `checks.types` reports 0 errors. Lane part A on each area: 2245,
2236 and 2238 passed.

- The public raw layer went with the adapter: `nanopynix.EvalState`,
  `Value`, `eval_file`, `open_store`, the flake functions and
  `input_from_*`. Tests of that layer use `_core`'s objects.
- `register_primop(name, arity, callback)`: huggorm's primop carries no
  argument names and no doc, so the call no longer takes them.
- The eval counters and `register_store_implementation` raise
  `NotImplementedError` until their ports land.
- `GCOptions` takes a union from 2.35 on. nanopynix picks the shape by
  `hasattr(huggorm_bindings, "GCSpecificPaths")`, and its pyright gate
  holds that name false (`defineConstant`), because it reads the
  stable stubs.
- A nanobind subclass hands its constructor's arguments to the C++
  constructor whatever its `__init__` does. `class Fake(Store)` called
  with `"local"` opened the real store. A fake store is
  `Fake("dummy://")`, told its name after construction.

**The one lane error is older than stage 2.** Every huggorm lane since
52 errors at the teardown of
`test_empty_path_raises_instead_of_aborting[local-add_temp_root]`:
`substituters` moved from the host's value to the test environment's.
The settings guard is function-scoped, and pynix's module-scoped
`populated_store` fixture runs an in-process pynix session, which
applies the host's `nix.conf` outside any test's window. The next test
that opens a session puts the environment's settings back, and the
guard blames it. Reproduced with the two tests alone in 12 s; without the
pynix test, 21 pass. Carl's call: pynix's fixture gets the test
environment's settings, by someone who may edit `pynix/tests`, and the
guard now reports a fixture's change at the setup of the test that asked
for the fixture.

**Eval counters: ported.** huggorm declares `EvalState.statistics_json`,
`eval_counters_enabled` and `set_eval_counters_enabled`, and carries the
three count-calls patches; nanopynix takes them from
`nixPatchesFor`, which also picks git's own remote-verbosity patch, and
dropped its copies.

**Nix git moved under huggorm when it took the lock.** huggorm's own
`<nixpkgs>` had Nix git 20260804; the umbrella lock has 20260912, and
four changes there broke the git build, found one compile at a time:

- `Derivation::inputs` is one `std::set<SingleDerivedPath>`.
  `FullInputs::fromSet` splits it back, as upstream's ATerm writer does.
- `Verbosity` is an `enum class`. `std::to_underlying` serves both kinds.
- `parseFlakeRef`, `FlakeRef::fromAttrs`, `Input::fromURL` and
  `Input::fromAttrs` take no fetcher settings. The three parse functions
  dropped their `settings` parameter on every version.
- The remote-verbosity patch's second hunk no longer anchors; git has
  its own copy.

Wrong first: I read the git lane as green because it passed on 0804.
It had never compiled against the Nix that nanopynix's git lane uses.

**Land order, Carl's call: land first, port `store_impl` next.** The
drop lands with `register_store_implementation` raising
`NotImplementedError`; nothing outside nanopynix uses it. tasks/084 is
the design that port needs, and its ownership and threading choices are
Carl's.

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
   `pynix develop`. `Store.read_derivation` answers a `Derivation`
   with typed accessors, whose outputs are a five-arm union, and
   `to_json()` is Nix's own document; `Store.add_derivation(json)` is
   `nix derivation add`. `nix develop`'s shell derivation is NOT a
   binding: `tests/test_derivation.py` builds one in Python from those
   generated operations alone (Carl: every Nix operation from the
   DSL, `nix develop` itself may differ). What nanopynix ships as
   `get-env.sh` still has to reach the consumer. Still missing:
   `eval_store` on `build_paths`.
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

## How the port must not break the consumers

Carl approved this 2026-09-26. The fallback is the local bookmark
`pre-huggorm-port` in nanopynix (`ce5ff758`), easykubenix
(`30079b9d`) and huggorm (`76c434ae`), and the umbrella git branch of
the same name (`ef00d58`).

pynix and easykubenix reach Nix through nanopynix's public API. Their
non-test code uses the async layer, plus the sync `current_system()`
and `list_settings()` (pynix `_impl/config.py`, `target.py`,
`_impl/develop.py`) and `get_env_sh_path`.

**The seam is wider than `_core`.** Measured at `ce5ff758`: 24
modules of nanopynix import `nanopynix_bindings`, most at module
level. Public modules do it too: `protocols.py` (`BuildMode`),
`settings.py`, `libstore.py`, `stores.py`, `store_impl.py`,
`get_env.py`, and the lazy table in `__init__.py`. Two places tell a
Nix error by its module name, `startswith("nanopynix_bindings")`
(`exceptions.py`, `rpc/_status_details.py`). So a variant with only a
huggorm `_core` cannot import nanopynix, every test errors at
collection, and the lane has no pass count.

1. One engine module in nanopynix. Every bindings import goes through
   it, and the two module-name checks become a check that module
   owns. Only the bindings engine exists at first, and the suite
   proves no behaviour change. This is the one step that can break
   pynix, so it lands and locks alone.
2. A build-time engine choice: a `-huggorm` variant scope beside the
   current one. Two libnix copies cannot share a process, so it
   cannot be a runtime switch. The engine module picks by which
   package the venv installs, and fails if it finds both or neither.
3. The Nix under both engines is the same. huggorm needs the
   interrupted-thunk patch, and nanopynix's 2.34 lane lacks it
   (nanopynix#309), so that lane takes it first. Otherwise a red
   cancel test measures a Nix difference, not the port.
4. A non-blocking CI lane runs nanopynix's suite and pynix against
   the variant. Its pass count is the progress measure; each failure
   is a huggorm task, never a consumer edit. easykubenix imports
   `sources.nanopynix` and takes the default scope, so the lane
   reaches it by handing it a `sources.nanopynix` that selects the
   variant, not by editing it.
5. Flip the default only when that lane is green; keep the bindings
   variant a while. The nixidae lock is the rollback.

## Where the port stands

Steps 1 to 4 landed 2026-09-26: `nanopynix._engine`, the #309 patch on
the 2.34 lane, `nanopynixForHuggorm` and the non-blocking
`test-huggorm-nix_2_34` job. `nanopynix/_engine_huggorm.py` answers
each engine name, and a name not ported is a placeholder that raises
`NotPortedError` naming it. The job's failures group by that name, so
the most common one is the next thing to port.

Carl's calls on the way:

- pynix's test harness reached the bindings for activity tracking.
  nanopynix got a public `Session.tracking_activities()` instead, and
  pynix's tests use it. pynix's source is untouched.
- Import does not read nix.conf. `load_config()` does, when the
  caller asks, as nanopynix's `init_libstore(load_config)` does.

Ported: `build_info`, `current_system`, the settings functions and
`init_libstore`. The lane then measured 1796 passed, 715 failed and
286 errors. The largest groups, in order: `expr.parse_nix_path` (727),
`store.render_store_reference` (67), `filter_ansi_escapes` (54),
`is_pseudo_url` (35), `signals.InterruptToken` (15). The logger and
verbosity group (`install_logger`, `set_verbosity`,
`set_logger_request_id`) is the first that needs a design, because
huggorm's logger is a pull model. `get-env.sh` still fails loudly.

Then `parse_nix_path`, `is_pseudo_url`, `render_store_reference` and
`filter_ansi_escapes` (the last in `terminal.py`, the first
declaration made of functions alone, which needed an emitter fix).
The lane: 1897 passed, 616 failed, 284 errors. The largest groups:
the rpc worker dies on `util.get_default_verbosity` (about 384
tests, reported as `Connection lost`), `get_default_verbosity`
in-process (361), `store.parse_store_reference` (49),
`signals.InterruptToken` (15).

## The logger: nanopynix pulls

Carl's call, 2026-09-26: nanopynix moves onto huggorm's queue, not
the other way round. An engine-neutral log source goes behind
`nanopynix._engine`, and the in-process engine and the rpc worker
drain it. huggorm gains, as declarations, what nanopynix's callback
logger does today: the activity filter (build and copy activities
and their results, at any verbosity, never dropped), a default
verbosity for new Nix threads, and per-thread verbosity. The request
id maps onto `begin_request`/`end_request`. pynix's `--nom` rides on
the activity path, so its tests are the gate for this step.

What landed, and how it differs from that sentence:

- huggorm declares `set_thread_verbosity`, `clear_thread_verbosity`,
  `thread_verbosity`, `set_default_verbosity`, `default_verbosity` and
  `current_request`. `subscribe_process_logs` sets the default through
  the same helper, so a default and its demand on `nix::verbosity` are
  one fact.
- nanopynix keeps its callback. The huggorm engine subscribes the
  process queue, and a thread drains it every 50 ms into the callback,
  in the arguments the bindings' logger passes. So nothing above
  `_engine` changed.
- The finalized marker is nanopynix's, not huggorm's. A queue can hold
  a call's records after the call returns, so
  `LogCollector.request_finalized` first calls the engine's
  `flush_logs`, and the marker cannot overtake its records. The request
  id uses `begin_request` alone; the pump skips huggorm's marker.
- No activity filter yet. huggorm sends every activity, and
  `set_activity_tracking` only records the choice. The filter is the
  next logger step, for volume and not for correctness.

Gaps, recorded and not fixed:

- huggorm renders `logEI` and `warn` as `"msg"`. nanopynix sends
  `"error"`, with the error's dict, and `"warn"`, and three readers
  depend on it: `pynix/_util.py:394`, `pynix/_build_monitor.py:310`
  and `nanopynix/models.py:364`. `tasks/032` holds the question.
- pynix's `--nom` tests also need `open_store`, so they cannot gate
  this step yet. The lane count is the measure until the store lands.
- huggorm's `Interrupted` is a `BaseException`, and
  `_run_with_log_context` translates only `Exception`. Only a cancelled
  call reaches it, and the executor's interrupt path catches it.

Lane after the logger: 1898 passed. The worker deaths and the
`get_default_verbosity` group were gone, and every call stopped at
`signals.InterruptToken`: each executor run makes one.

## Interrupt scopes

huggorm cancelled by `thread_request`, and nanopynix arms a token
around a call and then names the call's request inside it. The request
replaced the token, so a cancel matched nothing. Carl's call,
2026-09-26: huggorm gets an interrupt scope, not a token registry in
nanopynix. `begin_interrupt_scope`, `end_interrupt_scope`,
`cancel_interrupt_scope` and `forget_interrupt_scope` use a second
thread-local key and a second table, and the hook asks both. Two
tables keep scope N and request N apart; a test says so.

The token forgets its scope when the scope ends, because a kept
cancellation makes every `checkInterrupt` in the process take a lock.

Measured: before the port every rpc `Shutdown` failed on the token
before it ended the log stream, so each rpc test waited the 2 s log
drain timeout, and the lane took 17 minutes. After it: 4 minutes,
1916 passed, 597 failed, 284 errors. Every session now stops at
`expr.init_libexpr` (about 1170 records across the in-process, rpc
and wire forms), then `store.parse_store_reference` (49).

## The store opens

`init_libexpr` enables `fetch-tree`; huggorm starts the collector at
import. The evaluator thread hooks: `_enter_evaluator_thread` does
nothing, because huggorm registers a thread on its first evaluator
call, and `_exit_evaluator_thread` is `gc_release_thread`.

`nanopynix._core._objects.CoreStore` is the one caller of a raw store,
so the huggorm engine answers CoreStore's method names with an adapter
over huggorm's `Store`, converting at that boundary: absolute paths, an
SRI hash, the rendered content address and signatures. An unported
method raises `NotPortedError` naming `Store.<method>`.

huggorm gained, for it:

- `Store.reference()`, `store_dir()` and `close()`.
- `StoreReference`, its four-arm variant and `parse_store_reference`.
  `daemon?x=y` parses to `Specified("unix")`, not `Daemon`, and a
  test says so.

Four defects found on the way, each fixed with a gate:

- `parse_store_path("")` and `parse_derived_path("")` ENDED THE
  PROCESS: Nix's `canonPath` asserts a non-empty path. Both raise
  `BadStorePath` now.
- A union whose C++ type is the arms' `std::variant` itself got a
  caster that specialised the type it delegates to, and did not
  compile. `Variant(bare=True)` emits only a `static_assert`, which
  fails when the arms are out of order; checked.
- `pyinit` restated `nbemit.public`'s rule for factories and got its
  edge wrong, so `parse_store_reference` was bound and stubbed and
  missing from `__all__`. It reads `public` now.
- The binding stubs named every union alias, which no compiled module
  defines, so a typechecker read each union as unknown. They write the
  arms out now. A smoke-test gate refuses a stub annotation naming
  something the stub neither defines nor imports; it found
  `BuildError` unimported in `build_result.pyi` as well.

And one in nanopynix: pynix-lsp raised out of `did_open` when a
file's context could not open, so a client waited for diagnostics
until its deadline - 120 s per lsp test on this lane, hours in all.
Carl's call: fix pynix-lsp. It publishes the failure as a diagnostic
now.

Lane: 2044 passed, 575 failed, 181 errors, 4 minutes. The groups:
`expr.EvalState` (847), `Store.add_to_store` (154),
`expr.register_primop` (75), the flake registry (40).

## The evaluator opens

nanopynix's huggorm engine has an `EvalState` and a `Value` now. They
answer the raw names `CoreEvalState` and `CoreValue` call, over this
repository's `EvalState` and `Value`. Each read forces first, because
a `Value` here refuses a thunk. `auto_call` follows Nix's
`autoCallFunction`: defaulted formals, `__functor`, and anything else
unapplied, where `apply_auto` refuses.

Three changes here, each with a gate:

- `EvalState` takes a `Store`, not a URI (Carl's call). The URI form
  opened a second store, and `dummy://` opened twice is two empty
  stores. `test_the_state_shares_its_store` adds a path to a writable
  dummy store and reads it through the state, and a state over a second
  store opened from the same URI cannot. RPC callers acquire a `Store`
  first and pass the handle: the shape `test_remote` said `tasks/060`
  planned.
- `make_null`, the one JSON scalar the producers lacked.
- Arity 0 is a lazy constant, as Nix makes it (Carl's call). This
  reverses the refusal: nanopynix's API takes arity 0 and means that
  constant, and its shared test fixtures register one.

Two defects in nanopynix's side, both found by the lane:

- The log pump kept its lock across `fork()` and lost its thread. A
  child forked mid-drain waited on the lock for ever. At-fork hooks
  hold the lock across the fork and start a new pump in the child.
- The primop bridge held its evaluator, which holds the bridge. Only
  the cyclic collector frees that, and each store kept its database
  open until it ran: the lane crossed 1024 descriptors, prompt_toolkit's
  `select()` refused its pipe on every pass of the loop, and anyio's
  test runner kept each exception until the host swapped at 8 GB. A
  weak reference fixed it: 20 descriptors against 35 without.

Wrong on the way, and what refuted it: a flood of progress records
(31 a second, measured); an evaluator that leaks (300 states, 17 MB);
Boehm and `fork()` (0 of 40 children hung). Three later "hangs" were
leftover processes of lanes I had stopped: `TaskStop` ends the shell,
not a `systemd-run --scope` under it.

Not ported: the REPL (`begin_repl`, 31), the per-state setters,
`statistics_json`, `Value.build`, a primop that returns a callable,
primop argument names and docs.

Lane: 2276 passed, 366 failed, 170 errors, 4 min 26 s. The groups:
`Store.add_to_store` (154), `flake.parse_flake_ref` (50), the flake
registry (55), `EvalState.begin_repl` (31).

## The flake registry

2026-09-27. `decl/registry.py`: `registry_entries`, `user_registry_path`,
`registry_add`, `registry_remove`, `registry_pin`, and two produced
values, `RegistryEntry` and `RegistryWrite`.

Decisions:

- `Attr` is a TYPED union, `Str | U64 | Bint` (Carl's call over a JSON
  string). The DSL had no scalar arm, so it gained one: named by its
  wire spelling (`uint` keeps the width), refused when two arms are one
  Python type. The codec matches a scalar arm by EXACT type: with
  `isinstance`, the wire test got `{'yes': 1}` for `{'yes': True}`.
- Every call takes its own `settings`, over what the process has, and
  needs no evaluator. `cpp/fetch.hpp` builds them from `globalConfig`
  rather than from `settings.hpp`'s copy: a function-local static in a
  second extension module is a second, unregistered copy. The `eval`
  module registers the fetcher settings at import, and the package's
  front door imports it before any other module runs.
- A write reads the file it names with `Registry::read`, never
  `getUserRegistry`, which caches the first read for the process.
- A reference parses against `base`, None for the working directory.
- The flake feature is required, as Nix requires it: the first test
  run refused every parse with "experimental Nix feature 'flakes' is
  disabled".

Wrong on the way: the emitter included no header for a class that a
signature names from another declaration (`registry.cpp` got an
incomplete `nix::Store`), and the manifest took a free function named
by `@produced(by=...)` as a constructor even for a class with no
`__init__`. Both were fixed generically, in their own commits.

Not verified: `registry_pin` fetches, and the sandbox has no network,
so no test calls it. Nix writes an empty registry as `"flakes": null`.

Lane with nanopynix's registry adapter: 2350 passed, 355 failed, 107
errors, 4 min 36 s. The groups: `flake.parse_flake_ref` (52),
`EvalState.begin_repl` (31), `Store.read_derivation_typed` (13),
`Store.query_missing_typed` (10). nanopynix's type gate had only the
stubs on its path, so every vocabulary a stub names was Unknown to
pyright; it now carries the runtime package too, which `partial` asks
for.

## Flakes

2026-09-27. `decl/flakeref.py`: `FlakeRef`, a value made by
`parse_flake_ref`. `decl/eval.py`: `LockedFlake` and `LockedInput`,
and `EvalState.lock_flake`, `call_flake`, `get_flake` and
`flake_metadata_json`.

Decisions:

- `FlakeRef` is a VALUE. Nix 2.34's `Input` keeps no settings pointer,
  so nanopynix's owner of the parse settings (its #34, a 2.31 fix) has
  nothing to own here. Its one wire part is `to_attrs`; `dir` carries
  the subdirectory both ways.
- `flake::Settings` is REGISTERED, as the eval and fetcher settings
  are. Without it `accept-flake-config` from nix.conf went to
  `unknownSettings`; the new test read `None` with the line removed.
  `get_setting` lists no setting whose feature is off, so a test of a
  flake setting turns `flakes` on too.
- `LockedFlake` holds a share of the state's core, as a `Bridge` does,
  through `Evaluator::keep`. Affine, like `Value`.
- nanopynix's `update_inputs: bool | list[str]` is split into
  `recreate` and `update`: a union of a scalar and a container has no
  declaration, and two parameters say the same thing.
- The per-call settings helper is one template, `call_settings<S>`,
  for fetcher and flake settings alike.

Not done, and Nix does it: `flake::Settings::configureEvalSettings`
adds `builtins.getFlake`, `parseFlakeRef` and `flakeRefToString` to a
state. `nix`'s `main.cc` calls it.

Wrong: this said neither engine calls it. The other engine does, in
`nix_flake.cpp`, through `evalSettingsConfigurators`. The build port's
lane found the gap: pynix's nixpkgs import reads `parseFlakeRef`.
`apply_configured` now calls it with libcmd's `flakeSettings`, and two
tests fail with the call removed.

Wrong on the way: `FlakeRef(url)` as a constructor. A constructed
value's constructor must take its wire parts, and the build refused a
URL. The smoke comparison read nanobind's `Mapping[str, str]` for a
map parameter as a disagreement; it now treats `Mapping` as `dict`,
as it treats `Sequence` as `list`.

Lane with nanopynix's flake adapter: 2391 passed, 314 failed, 107
errors, 5 min 43 s. Left: `EvalState.begin_repl` (31),
`Store.read_derivation_typed` (14), `Store.query_missing_typed` (10).
Four pynix flake tests still fail; one reads `has_attr` on a value
that is not a set, and huggorm raises a bare `RuntimeError` ("value is
not attrs") where the other engine raises Nix's own type error from
`forceAttrs`. That is an error-type gap in every `Value` accessor, not
a flake one.

Closed the same day: `errors.py` declares `EvalError` and
`NixTypeError`, and the emitted guard throws `nix::TypeError` with
`forceAttrs`'s words ("expected a set but found an integer"). An
evaluation failure now crosses as `EvalError`, not the root
`NixError`, which two smoke checks and three tests had pinned.
nanopynix maps an engine error through its MRO. Lane: 2432 passed,
276 failed, 107 errors.

## The REPL scope

`EvalState.repl()` makes a `Repl`: one `nix repl` scope, affine, with
`process_line`, `eval_expr`, `eval_file`, `load_file`, `add_attrs`,
`names` and `select`. The bodies follow `NixRepl` in libcmd's
`repl.cc`: bindings first, then with a `;` appended, then an
expression. Each scope is its own environment, so a state can hold
several; the other engine held one per state.

Decisions:

- Nix's own error when the scope is full: `nix::Error("environment
  full; cannot add more variables")`. The other engine had its own
  `RuntimeError`; it now throws Nix's too.
- One divergence, on purpose. `addAttrsToScope` refuses a set when
  `displ + size >= envSize`, one slot short of the allocation. This
  takes a set that fills the scope exactly, as the other engine did.
  The environment has 32768 slots, so this is no more permissive
  than what it binds.
- An optional wrapped return. `process_line` answers `Value | None`
  and `select` answers `ReplSelection | None`. The emitter refused
  both ("none of them adopts nothing"). Carl chose to teach it:
  `wiretypes.adoptee` names the class a return adopts, the async
  wrapper passes None through, and the client makes a proxy for the
  inner class. The server and the codec needed nothing, because a
  handle is a message and has presence.
- The root. `allocEnv` is collector memory and the struct is in the
  Python heap, so the pointer lives in a traceable allocation, as
  `allocRootValue` keeps a value. `gc_stats()["scopes_collected"]`
  counts finalized environments, because a binding read back cannot
  show a freed block.

Wrong on the way: the first root test read a binding back after
collections and churn. With the root broken (`std::allocator`) it
still passed, twice: once on this thread, and once with the scope
made on a thread that exited. The finalizer count is what the other
engine settled on for issue #70, for the same reason. With the count,
the broken root fails: `assert 16 == 15`, the held scope freed.

## Linking libcmd

A file argument is what `nix eval --file` takes: `<nixpkgs>`,
`flake:x`, a tarball URL, or a path. `lookupFileArg` reads it, in
libcmd. Carl chose to link libcmd over a copy of its four branches.
`EvalState.eval_file`, `Repl.eval_file` and `Repl.load_file` call it.

libcmd registers its own `evalSettings`, `fetchSettings` and
`flakeSettings` on `globalConfig` when it loads. `settings.hpp`
registered a second copy of each, and five settings tests failed:
`GlobalConfig::set` stops at the first object that takes a name, so
the copies never saw a value. huggorm now reads libcmd's objects, as
the `nix` CLI does, and `_settings_init` is gone: the registration
happens at load, before anything can read nix.conf.

Lane with nanopynix's REPL adapter: 2450 passed, 258 failed, 107
errors, 5 min 57 s. Every pynix REPL test passes. Left in the REPL
area: `Value.get_doc` and `Value.edit_location`, which `:doc` and
`:edit` read. Next by count: `Store.read_derivation_typed` (14),
`Store.query_missing_typed` (10), `Store.get_store_dirs` (8).

## Derivations, missing paths and store directories

`Derivation.input_drvs` raised for dynamic derivations. nanopynix
reads the whole tree, and two fidelity tests prove it. `InputDrvNode`
now binds `DerivedPathMap`'s `ChildNode` as a wire value that holds
itself. The generator needed nothing new for the recursion. The set
member needs `collection=`, because `_from_parts` gets a vector.

`Derivation` still has no RPC surface: it is a proxy with no service,
and the build warns so. nanopynix's RPC runs through its own worker,
so the lane does not need it. A remote test of the node was written
and dropped for that reason; the round-trip test covers its parts.

`Store` gains `root_dir`, `state_dir`, `log_dir`, `real_store_dir`
and `build_dir`, each None for a store with no filesystem.
`query_missing` needed only an adapter: `parse_derived_path` existed.

Lane with the three adapters: 2478 passed, 236 failed, 107 errors,
6 min 0 s. Next by count: `EvalState.set_eval_setting` (7),
`Store.dump_db` (5), then `BuildMode`, which fails every build test
through `build_mode_value`.

## Building

`Store.build_paths` and `build_paths_with_results` take `eval_store`,
and `Value.output_paths` is `PackageInfo::queryOutputs`. nanopynix's
`Value.build` needs both.

Wrong turn: `output_paths` was first documented as None for a
content-addressed output. Nix's `queryOutputs` raises there instead:
the `outPath` is a placeholder, not a store path. A test states it.

Wrong turn: the eval-store test first used `FIXED`'s hash, which is
not the hash of the empty file it added. The builder ran. The test
now takes the hash from the path it adds.

The lane also found a nanopynix adapter bug: `Value.auto_call`
returned `self`, so two `CoreValue`s shared one engine value and one
close released it for both.

Lane: 2542 passed, 208 failed, 72 errors, 6 min 27 s. Left in the
build tests:

- `builtins.parseFlakeRef` is missing, because the huggorm engine
  does not turn on the default experimental features at init.
  `test_init_entry_point_enables_the_default_experimental_features`
  names it. Two build tests import nixpkgs and fail on it.
- `test_build_keeps_the_message_of_nix_for_any_other_value` expects
  "selected value is not a derivation". Nix never says that; the
  other engine's binding wrote it (`nix_expr.cpp:586`), and pynix's
  test calls it Nix's message. huggorm says "the value is not a
  derivation".

Lane with the flake builtins: 2642 passed, 150 failed, 31 errors,
17 min 11 s. The time grows because the tests that import nixpkgs now
evaluate it. Next by count: `EvalState.set_eval_setting` (7),
`Store.dump_db` (5).

## Live settings and the database dump

`EvalState.set_setting` changes a live state's own settings, the
evaluator's first and then the fetcher's, as the constructor tries
them. nanopynix's two setters both call it.

Its test found a second gap: `max-call-depth` throws
`StackOverflowError`, which derives `EvalBaseError`, not `EvalError`.
huggorm declared no `EvalBaseError`, so it reached Python as the base
`NixError`. It is declared now, with `EvalError` below it, as upstream
has them. nanopynix maps that name to its own `EvalError`.

`Store.make_validity_registration` is `nix-store --dump-db`.

The new store directories and `Value.output_paths` have no RPC form:
`pathlib.Path` and `StorePath | None` have no wire policy. The build
warns about each, as it does for `real_path`.

Lane: 2652 passed, 140 failed, 31 errors, 16 min 38 s. Next by count:
`Store.optimise_store`, `Store.add_perm_root`, `get_env_sh_path` and
the settings metadata JSON, three each.

## Roots, optimising, get-env.sh and the settings documents

`Store.add_perm_root` and `Store.optimise_store` are bound. The first
refuses a store with no filesystem, as `collect_garbage` does. The
second keeps `nix::Store`'s default, which does nothing and says
nothing.

Wrong turn: the first docstring said libstore takes a relative
`gc_root` as given. `IndirectRootStore::addPermRoot` calls
`canonPath`, which refuses a relative path and ASSERTS on an empty
one. The body now refuses the empty string itself, as
`parse_store_path` does.

`eval_settings_json`, `fetch_settings_json` and `flake_settings_json`
describe libcmd's three settings objects, one each. `settings_json`
describes all of `globalConfig`, which holds all three.

`huggorm-bindings` carries Nix's `get-env.sh`. Nix compiles it into
the `nix` binary, so no library holds it. The package copies it from
the source of the Nix it links, as nanopynix-bindings does.

## Documentation and editor locations

`Value.doc` answers a `Doc`, or None, for any value. It answered a
string before, and "" for no documentation. `getDoc` also documents a
functor set, by applying `__functor`, so the function guard is gone
and the body forces the value.

`Value.attr_doc` answers where a set defines an attribute, and the
doc comment there. `Value.edit_location` answers the file and line
`nix edit` opens.

A position's file is `huggorm::position_file`. For a string or stdin
it answers Nix's own name, which `Pos::print` writes before the first
':'. pynix prints that name in `:doc`, so None would show "None:1".

Measured, not guessed: an anonymous lambda's `Doc.name` is "", not
None, because `getDoc` sets it to an empty name. A doc comment's
inner text keeps the newline after the comment.

## The lane's memory

The lane died at its 6 GB scope cap. Measured: each `pynix search`
index build keeps about 90 MB, the same on both engines, and a forced
collection between tests returns nothing. That is nanopynix#310, not
this port. The lane now runs as two pytest runs.

Lane, as `-k 'not (pynix and not nanopynix)'` and `-k 'pynix and not
nanopynix'`: 2680 passed, 137 failed; peaks 2.7 GB and 3.7 GB, 18 min
23 s together. Next by count: `Store.write_dev_shell_derivation` (9),
then `Store.verify_store`, `Store.add_indirect_root` (3 each) and
`Store.find_roots`.

## Dev shells, verifying and the root list

`write_dev_shell_derivation` needs no new binding. nanopynix's adapter
follows `getDerivationEnvironment` over the derivation's JSON, and
`add_derivation` fills in the deferred output paths. nanopynix-bindings
does the same in C++ with a branch per Nix version; `add_derivation`
makes that branch unnecessary. The 25 `test_develop` tests pass.

`Store.verify_store`, `Store.add_indirect_root` and `Store.find_roots`
are bound. `find_roots` answers `GcRoot` records, one per link, sorted
by link, because libstore's `Roots` is a hash map of path to links.
`censor` hides the link names only: libstore reads `/proc` either way.

Wrong turn: `GcRoot` was declared in `gc.py`, beside `GCResults`, and
the build failed with "'GcRoot' is not a member of 'huggorm'" in
`store.cpp`. A generated struct is emitted in the module that declares
it, so it moved to `store.py`, the module of its producer.

Each new test was proved by a break: no sort, `censor` forced off and
`check_contents` forced off turned 3 tests red.

Lane: 2692 passed, 125 failed (1816/70 and 876/55), 17 min 15 s. No
`Store.*` name is left in the NotPorted grouping. The next by count is
"cannot yet make a Nix value from function" (16): a Python callable
passed as a Nix value.

Part A's scope peaked at 5.1 GB, then 4.0 GB on a second run with the
same 70 failures; it was 2.7 GB before. The pytest process's RSS stays
under 957 MB, so the peak and its spread come from outside that
process. Part B peaked at 4.1 GB.

## A Python callable as a Nix function

`EvalState.make_primop` answers an anonymous primop value, so a primop
implemented in Python can return a function. nanopynix's ipaddress
primops return sets of them. `register_primop` and `make_primop` share
one body, the `primop_impl` helper, which names the function in its
errors: `builtins.<name>` for a registered one, the given name for a
made one.

The callable lives on the core, as a registered one does, so the
collector sees it through `evaluator_tp_traverse`. It lives as long as
the state, so a primop that makes a function on every call grows the
state by one callable each time. nanopynix-bindings keeps every such
callable in a process-wide registry, so this is no worse.

Arity 0 is refused. nanopynix's adapter calls a callable with no
parameters at once, as the other engine does.

Proved by breaks: the `builtins.` label, no arity guard, and an extra
reference to the callable turned 3 tests red.

Lane: 2700 passed, 117 failed (1824/62 and 876/55), 16 min 36 s; both
scopes peaked at 4.6 GB. The 8 ipaddress tests are the whole
difference, and no `NotPortedError` is left. The remaining failures
are small groups, the largest 8 tests: "value is not a list or an
attribute set", from nanopynix's `test_scalar_accessor_semantics`.

## Nix's errors from the collection accessors

Three accessors raised a bare `RuntimeError`, which a caller that
catches Nix's errors cannot tell from a bug. pynix-lsp is that caller:
it catches `NixError` around attribute lookups, so the `RuntimeError`
escaped it.

- `size` raises Nix's `TypeError` for neither collection, as a guard
  does, naming both kinds.
- `get` raises `MissingAttribute`, with Nix's words and suggestions.
- `at` raises `ListIndex`, naming the index and the size.

`MissingAttribute` and `ListIndex` are huggorm's own classes, as
nanopynix-bindings' are. Nix reports both only from inside the
evaluator, as a plain `EvalError`. A caller of `get` must tell a
missing name from a failed evaluation, and nanopynix maps each to a
class that is also a `KeyError` or an `IndexError`. Parsing the
message was the alternative, and it is banned.

Their C++ classes are a helper, `cpp/eval_errors.hpp`. libexpr
instantiates `EvalErrorBuilder` for its own classes only, so
`state.error<huggorm::MissingAttribute>` would not link.

`length` and `names` are guarded versions of `size` for one kind.
nanopynix's `list_length` and `attr_names` must refuse the other kind,
and `size` of an empty list answered 0 where `attr_names` must raise.

Proved by breaks: a plain `EvalError` from `get` and `at`, and no
suggestions, turned the tests red.

Lane: 2764 passed, 53 failed (1835/51 and 929/2), 18 min 30 s; peaks
4.9 GB and 3.8 GB. Part B went from 55 failures to 2, and no test
failed that passed before.

## A primop's exception, builtins.sleep and the errors namespace

A Python primop's exception reached Nix as `nb::python_error::what()`,
which carries the Python traceback. Now one of huggorm's typed errors
(it has `to_dict`) shows its message bare, as a C++ primop's
`EvalError` does, and any other class reads "Class: message". The
helper is `primop_failure` in `cpp/eval.hpp`.

nanopynix's bridge raises huggorm's `EvalError` for `PrimopError` and
`ValueError`, which the other engine shows bare. `builtins.sleep` is a
Python primop in each state: nanopynix-bindings adds it in C++ for the
cancel tests, and `time.sleep` polls no Nix interrupt either.

The engine's `errors` namespace answers `Error` and `BadStorePath`.
`stores.parse` catches the first to report a bad URI as a `ValueError`.
`as_float` widens an integer, as `forceFloat` does; huggorm's
`floating` reads one kind.

Lane: 2784 passed, 33 failed (1855/31 and 929/2), 15 min 7 s; peaks
5.1 GB and 4.1 GB. No test failed that passed before. Four of the rest
need `tasks/100`, an error that carries Nix's `ErrorInfo`.

## nix.conf after a reset, and the activity filter

A state and a one-call settings object copied only the settings marked
overridden. nanopynix calls `reset_overridden` right after loading
nix.conf, to tell the file's settings from its own, and the reset
clears the mark and keeps the value. So `pure-eval` and
`flake-registry` from the file reached nothing. Both now copy every
value that differs from the fresh object's own. The `nix` CLI reads
the global objects' values and never the marks, so this is the closer
mirror.

Found by measurement: `eval_settings_json` said `pure-eval` was true
when the state was made, huggorm alone honoured it after
`load_config`, and it stopped doing so once `reset_overridden` ran.

nanopynix's adapter now filters activities in its log pump, as
nanopynix-bindings' `ActivityTracker` does in C++. The pump drains
huggorm's queue on a thread of its own, so the filter costs the
evaluation nothing.

Lane with the settings fix: 2788 passed, 29 failed (1859/27 and 929/2);
peaks 4.2 GB and 3.6 GB.

## apply and functors, wordings, and experimental features

`Value.apply` had `@guard("function")`. `callFunction` checks the type
itself and reads no payload first, and it calls a set with
`__functor`, as `f x` does. So the guard refused only a call Nix
makes. It is gone, and a non-function raises Nix's own "attempt to
call something which is not a function".

`drv_path` and `output_paths` say "selected value is not a
derivation". Nix has no one wording: each caller of `getDerivation`
writes its own. That one is nanopynix-bindings', and pynix reads it.

`enable_experimental_feature` inserts a feature and leaves the
setting's overridden mark, as nanopynix-bindings does. Through
`extra-experimental-features` nanopynix's own default features showed
up as the caller's change to the host's nix.conf.
`is_experimental_feature` answers whether Nix knows a name: the setting
only warns about one it does not know, and nanopynix raises.

nanopynix's bridge names the primop and the type when an argument
holds a function, in the other engine's words.

Lane: 2798 passed, 19 failed (1867/19 and 931/0), 17 min 3 s; peaks
2.4 GB and 4.0 GB. Part B, pynix, passes whole. Of the 19:

- 13 import `nanopynix_bindings` themselves: the meta tests of stub
  patterns, the soak roster and the docs, and one init test. They
  pass only when nanopynix stops shipping that package.
- 5 need `tasks/100`, Nix's `ErrorInfo` on an error.
- 1 is a namespaced worker. `unshare(CLONE_NEWUSER)` fails with EINVAL
  in a process with more than one thread, and importing huggorm starts
  15 Boehm marker threads (`GC-marker-0` to `-14`, read from
  `/proc/self/task`). nanopynix-bindings starts none at import: its
  `init_libexpr` starts the collector.

## Nix's ErrorInfo on an error

`tasks/100` is done. A huggorm error carries `info`, an `ErrorInfo`
record with the position, the trace and the suggestions, and it
crosses huggorm's wire typed. nanopynix reads it through each engine's
`error_detail`, into the dict `NixError.info` documents.

Lane: 2803 passed, 14 failed (1872/14 and 931/0), 20 min 11 s; peaks
1.8 GB and 3.7 GB. Part A fixes the five `info` tests and adds no
failure. Of the 14, 13 import `nanopynix_bindings` and 1 is the
namespaced worker.

## No thread at import

`tasks/101` is done. Importing huggorm starts no thread; the first
evaluator starts the collector's markers. That needed a bdwgc patch,
which nanopynix's huggorm scope carries too.

Lane: 2804 passed, 13 failed (1873/13 and 931/0), 17 min 58 s; peaks
4.5 GB and 4.1 GB. The 13 import `nanopynix_bindings`, and pass only
when nanopynix stops shipping that package.

## Tests that never ran on huggorm

Fifteen nanopynix test modules imported `nanopynix_bindings` at the top,
so on the huggorm lane they did not collect, and their tests counted as
nothing. They now take the engine from `nanopynix._engine`. On the first
run, 352 more tests passed, and 43 failed on what huggorm had not ported.

What huggorm gained for them:

- libexpr's error classes: `ParseError`, `UndefinedVarError`,
  `NixAssertionError`, `ThrownError`, `Abort`, `UnimplementedError`.
  A `throw` raised a plain `EvalError`.
- `log_message(level, text)`: one message through `nix::logger`.
- `start_collector()`: nanopynix's session starts the collector as the
  other engine does, so `NIX_PATH` reaches `nix-path` at session start
  and not in some later test.
- `collector_owner_thread()` and `gc_stats()["non_gc_bytes"]`.

nanopynix gained a `nix_engine(name, reason)` marker for the tests of
nanopynix-bindings' own artifacts: its API pages, its stub patterns, and
its REPL environment probes. Each names what huggorm has instead, or why
the test cannot apply.

Still open, from the lane:

- DONE: a primop argument with string context. huggorm has
  `realise_json`, `string_context` and `make_string(value, context)`,
  and the bridge builds what an argument names and gives every
  returned string the input's context, as nanopynix-bindings does.
- DONE: `fetchers.Input`: huggorm binds `nix::fetchers::Input`, in
  `FlakeRef`'s shape.
- DONE: `list_store_types_json` is `store_types_json`.
- DONE: two stores on one directory each keep their own temp-roots
  file, by a Nix patch (Carl's choice over a per-URI cache).
- The error event of the log stream carries no structured payload:
  `tasks/103`.
- The verbosity ceiling: `tasks/102`.
- The settings-leak guard stopped firing once `start_collector` put
  `NIX_PATH` into `nix-path` at session start; no run since has shown
  it.

## The verbosity ceiling: measured at CHATTY

nanopynix-bindings writes `nix::verbosity` once, at CHATTY, before any
Nix thread exists; its `nix_util.cpp` measured the ceiling for
evaluation, store queries and flake fetches. huggorm moves the gate as
subscriptions ask, because `RemoteStore::setOptions` sends the gate to
the daemon and the client re-raises each daemon line as `printError`
(tasks/095).

The probe (`.scratchpad/097/chatty_probe.py`): a daemon store, Nix
2.34.8 on both sides, 16 real paths, 20 rounds of `is_valid_path`,
`query_path_info` and `compute_fs_closure`. The gate is held by a
subscription on another thread, and the count is Nix's lines on the
caller's stderr. Three processes per level, identical each time:

| gate | lines |
| --- | --- |
| INFO (none held) | 0 |
| CHATTY | 0 |
| DEBUG | 32, all `performing daemon worker op` |

So query work costs nothing at CHATTY. Libstore holds 24 sites at
TALKATIVE or CHATTY, in GC, file transfer, substitution and the
builders; on a daemon those would reach a caller's stderr as errors
when the daemon does that work. Not measured.

## Lane after the temp-roots patch

Part A: 2293 passed, 6 failed, all on the open list above. Part B, the
first time: 16 failed, all pynix search tests, on `opening file
'/nix/store/...-source/nix/wire.nix': No such file or directory` inside
the test store, and 32 skipped. The failures did not come back: the
search tests alone passed on the patched build and on the one before
it (131 each), and a second full part B gave 899 passed and 0 failed.
Not explained. The 32 skips are the pynix-lsp tests, which skip when
they cannot fetch a schema; `lillecarl.cachix.org` answered that a NAR
it lists does not exist.
