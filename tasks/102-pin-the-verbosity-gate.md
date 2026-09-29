# Pin the verbosity gate, and tell the daemon something else

**OPEN.** Found by the nanopynix port (`tasks/097`), 2026-09-29.

## Decision

Carl, 2026-09-29, from three routes: pin `nix::verbosity` at CHATTY
once, at import, and patch Nix so `RemoteStore::setOptions` does not
send the pin to the daemon.

## Why

`nix::verbosity` is a plain global. Every `printMsg` site reads it on
its own thread, and huggorm's `VerbosityDemand` writes it whenever a
subscription starts or ends. nanopynix-bindings found that race with
ThreadSanitizer and pins the global instead: one write, before any Nix
thread exists. Its `nix_util.cpp` measured CHATTY as the widest pin
that costs evaluation, store queries and flake fetches nothing; DEBUG
costs a flake evaluation its RPC deadline.

huggorm did not pin because of the daemon (`tasks/095`):
`setOptions` sends the gate at handshake (`remote-store.cc:118`), the
daemon narrates at that level, and the client re-raises each line as
`printError`, so it reaches an unsubscribed caller's stderr as an
error. Measured for this task (`tasks/097`, "The verbosity ceiling"):
query work echoes 0 lines at CHATTY and 32 at DEBUG. Libstore holds 24
sites at TALKATIVE or CHATTY in GC, file transfer, substitution and the
builders, so a CHATTY pin would echo those when the daemon does that
work. The `setOptions` patch removes that.

## Decisions, 2026-09-29

Carl answered both open questions:

- **Above the pin:** an import-time ceiling, as nanopynix-bindings has.
  `HUGGORM_LOG_CEILING` at import picks the pin, default CHATTY. A
  subscription above it gets nothing above it, and `process_verbosity`
  says what the pin is. huggorm's widening test from `tasks/089`
  becomes "up to the ceiling".
- **The daemon:** the Nix patch makes `setOptions` send a separate,
  atomic level, and `VerbosityDemand` keeps that at the widest level a
  subscription asks for (INFO when none). So a DEBUG subscriber still
  gets daemon DEBUG lines, as now, and no pin reaches the daemon.

## Open questions for the work, now answered above

- What a subscription above CHATTY gets. With the gate pinned, Nix
  drops a DEBUG record before any logger runs. nanopynix-bindings reads
  `NANOPYNIX_LOG_CEILING` at import for that. huggorm's `test_logs`
  pins the widening that `tasks/089` added.
- What the daemon is told: INFO always, or the widest level a
  subscription asks for, which is what `VerbosityDemand` gives it now.
- nanopynix's huggorm scope needs the Nix patch too, as it has
  `libstorePatches`.

## Evidence

nanopynix's `test_nix_filters_at_a_pinned_ceiling_that_no_call_moves`
fails on huggorm: the ceiling is INFO, and the test expects CHATTY and
no movement.

## The work

- `nix-remote-verbosity.patch` adds `std::atomic<int>
  nix::remoteVerbosity{-1}` to libstore. `setOptions` sends it when it
  is zero or more, and `nix::verbosity` otherwise, so `nix` itself
  does not change. It is in `libstorePatches`, which nanopynix's
  huggorm scope already takes.
- `install_log_tap` is the one write to `nix::verbosity`: the value of
  `log_ceiling()`, at import, before any Nix thread exists. It sets
  `remoteVerbosity` to lvlInfo in the same place.
- `log_ceiling()` reads `HUGGORM_LOG_CEILING`: a level name in any
  case, or 0 to 7. A value that names no level REFUSES the import.
  nanopynix-bindings keeps its default for a bad value instead; that
  ignores the caller with nothing said, which is this repository's
  named failure mode.
- `VerbosityDemand::reconcile` stores into `remoteVerbosity`, never
  into `nix::verbosity`. Its floor is lvlInfo, a constant: the old
  floor read `nix::verbosity`, which is now the pin.
- `daemon_verbosity()` reads `remoteVerbosity`. The `test_logs` gates
  that watched `process_verbosity()` move now watch it, and two new
  gates pin `process_verbosity()` at CHATTY through subscriptions at
  7 and refuse a record above CHATTY to a subscription at 7.
