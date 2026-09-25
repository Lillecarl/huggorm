# What a dropped rpc stream cancels

**OPEN.** Undecided on purpose. `tasks/097` made a cancelled await stop
the Nix work under it: the runtime marks the request cancelled, and
Nix raises `Interrupted` at its next `checkInterrupt`.

The rpc server calls the same wrappers. So a server handler that
grpclib cancels also cancels its Nix work, and that happens when a
client drops its stream. Nobody has decided whether that is the rule.

## Two questions

1. **Should a client that goes away stop its call?** For a one-shot
   eval, yes: nobody reads the answer. For a service whose state
   outlives its callers (`CLAUDE.md`), a second client may be waiting
   for the same result. The eager background evals of `tasks/086` run
   in the server's own task group and not on a client stream, so they
   are not affected either way.
2. **May one client cancel another's request?** No rpc exists for it.
   The request id is process-wide, so one could. The precedent is
   `unsubscribe_logs`: a shared handle grants that power to everyone
   who holds it.

## What is measured

Nothing on the rpc side yet. `tests/test_cancel.py` covers the
binding and the in-process async wrapper.
