# Handle table lifecycle: leased graph with connection-backed holders

Review findings 2 + 13, shaped by Carl's design calls (2026-08-23,
revised same day): lifetimes form a GRAPH, holders are CONNECTIONS,
and disconnect reaps what only the dead connection held.

## Driving use cases

1. Ansible: controller launches the server, evaluates, forks workers.
   No connection sharing across fork (and none wanted): each worker
   opens its own connection. The controller - still alive - hands
   over endpoint plus handle IDs; workers continue without
   reevaluating. When the controller's connection later dies, handles
   ONLY it held are reaped; handed-over ones survive.
2. Evaluation server (see tasks/016): a persistent EvalState serves
   many connections over a long life. State handles must outlive
   whoever created them.

## Model

- Holders are connections, identified by a token. Acquire and each
  proxy-typed method return credit one lease to the calling
  connection. Possessing the handle ID remains the access capability;
  the token decides LIFETIME, not access. Three ways a lease ends:
  explicit Release, connection death, or Detach into escrow (which
  ends ownership but not existence).
- Disconnect = implicit Release of every lease the connection held.
  Entries reaching zero leases drop, freeing wrapper -> bridge cell ->
  GC memory. Drops CASCADE down the producer graph: a Value or
  Derivation pins its producing EvalState/Store; reaping children
  lets a parent with no leases and no live children go too.
  ("Values keep their EvalState alive.")
- Handover for fork: two styles. SHARE executes while the granting
  connection lives, moving (transfer) or duplicating (copy) a lease
  onto another connection's token. DETACH ("disconnect and keep my
  shit") releases the connection's claim WITHOUT dropping the entry:
  the lease goes into escrow, keeping the handle - and through the
  graph, its producers - alive with no owner. A later connection
  CLAIMS escrowed handles (presenting their IDs) and adopts the
  leases. Detach is what lets a creator exit entirely - evaluation
  server clients that create state, leave, and return tomorrow.
  OPEN QUESTIONS: transfer vs copy default for Share; how recipients
  present tokens; per-handle vs whole-connection Detach.
- Escrowed leases are unowned and never auto-reaped; this is where a
  per-lease TTL knob would bite (default off).
- Force-close: explicit aclose on a handle with live dependents.
  OPEN QUESTION: refuse, or cascade-kill dependents. Must mirror the
  in-process layer's aclose semantics.
- Crashed-but-alive TCP (half-open) leaks until transport-level
  keepalive fires; a per-lease TTL is a future knob, default off.

This SUPERSEDES both earlier sketches (per-connection exclusive
ownership; pure wallet-based leases disconnected from connections).

## Consequences

- Server tracks per-connection lease sets; transports (014) stay
  swappable as long as every transport surfaces "connection died"
  and "connection identity" to one dispatcher.
- Long-lived connections accumulate intermediate Value/Derivation
  leases from every proxy return; clients should Release eagerly -
  harness/tests must model this (see 012).

Not building yet. Blocks 014 (transport shims); feeds 016.
