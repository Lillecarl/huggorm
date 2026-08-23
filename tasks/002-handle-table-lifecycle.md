# Handle table lifecycle: refcounted leases

Review findings 2 + 13, redirected by Carl's design call
(2026-08-23): lifetimes belong to LEASES, not to connections.

## Driving use case

Ansible: the controller process launches the RPC server, evaluates,
then forks workers. It hands each worker the endpoint plus some
existing handle IDs; workers open their OWN connections and keep
using those objects without reevaluating. One connection/session is
therefore NOT the exclusive owner of any lifetime.

## Model

- The handle table maps id -> wrapper, plus a refcount of leases.
- Every handle crossing the wire grants exactly one lease: Acquire,
  and each proxy-typed method return, add one ref credited to the
  caller's lease wallet.
- A lease wallet is an opaque client-chosen identity, not a
  connection. Sharing = handing another process the endpoint, the
  wallet name, and handle IDs; it presents the same wallet when
  releasing. Forked workers need no special support beyond this.
- Release(handle, wallet) decrements; zero leases drops the entry and
  frees the wrapper -> bridge cell -> GC memory chain.
- Connection loss reaps NOTHING. A crashed client leaks until its
  leases expire or are released; a per-lease TTL is a possible future
  knob, default off.

This REPLACES the earlier sketch in this file (per-connection Session
ownership, reap on disconnect) - that model breaks the fork-sharing
scenario.

## Consequences

- Authorization is capability-style: possessing the handle ID lets you
  call it. Fine for localhost mock; real auth is a separate concern.
- Server needs no per-connection state at all - transports (014)
  become trivially swappable.
- Client: RemoteObj.aclose() Releases its own lease only; wallets let
  a group of objects release together.

Not building yet. Blocks 014 (transport shims).
