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

## Open questions for the work

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
