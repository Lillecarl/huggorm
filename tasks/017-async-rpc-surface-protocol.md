# Protocol/ABC unifying the async API and the RPC API

Carl's call (2026-08-23): the async wrappers and the RPC client
surface will share API shape in practice, so user code must be able
to consume either interchangeably. remote.py already promises
"identical semantics, different location" - this task makes it a
typed contract instead of a docstring.

## Goal

One typing.Protocol (or ABC) per wrapper class, satisfied BOTH by
generated Async* classes and by remote.RemoteObj-backed handles, so
`def drive(store: AsyncStoreLike)` accepts an in-process AsyncLocalStore
and a gRPC handle with no branching.

## Design questions

- Protocol vs ABC: structural Protocols need no inheritance from
  generated or hand-written sides and fit runtime-checks-free typing;
  an ABC buys isinstance() checks at the cost of forcing both sides
  into a shared base. Lean Protocol; revisit if runtime dispatch is
  wanted.
- Return-type divergence is deliberate: local returns AsyncStorePath /
  real sync copies, remote returns RemoteObj handles / deserialized
  copies. The protocol needs parameterization (Generic over the
  handle type) or deliberately loose returns - decide which lie is
  acceptable.
- Surface parity is real but policy-shaped: both sides derive from
  the same manifest, including drops (LocalStore has no
  query_derivation anywhere). Parity holds per class, not globally.
- RemoteObj resolves methods via __getattr__, so it satisfies a
  Protocol only nominally; plan an explicit declaration or a runtime
  conformance check in the harness.
- Emission home: generate protocols from the manifest alongside the
  wrappers (same pattern as the symbol-contract gate) so drift between
  protocol and surface is impossible.
- Synergy: makes 013 (mypy) meaningful for consumers; 018 defines the
  hierarchy the protocols should mirror.

Acceptance: one demo/test function typed against the protocol runs
the same calls against both an in-process wrapper and a remote handle.
