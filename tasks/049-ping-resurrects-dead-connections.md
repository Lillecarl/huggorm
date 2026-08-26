# Ping resurrects a swept connection instead of reporting death

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

The Session/Ping handler calls `self.table._conn_for(req.token)`
(server.py). `_conn_for` CREATES a connection when the token is
unknown. So after the sweeper reaps a silent client:

1. The client's leases are released and its handles may drop.
2. Its next Ping recreates an empty connection under the same
   token and answers `ok=True`.
3. The client keeps pinging happily and discovers its death later,
   as "unknown handle" on some unrelated call - the one moment
   nobody is looking for a lifecycle bug.

`_require_conn` exists for exactly this and raises for an unknown
token; Ping just does not use it. The client's `_ping_loop` also
swallows every exception (`except Exception: pass`), so even a
raising Ping would die silently today.

Two smaller inconsistencies sit beside it:

- Ping carries the token in the request BODY (`PingReq.token`);
  every other rpc carries it in the `x-nixmock-conn` metadata.
  One channel is enough, and the metadata one is the one lifecycle
  documents as the convention.
- Ping is also the only Session rpc reaching a private method
  (`_conn_for`) from outside lifecycle.py.

## Fix sketch

- Ping resolves the token through `_require_conn`. An unknown token
  becomes a typed error the client can see.
- The client's ping loop treats that error as "I was swept":
  surface it - re-bind, or fail the client loudly - rather than
  retrying forever. What the right reaction is (auto-rebind loses
  handles silently; raising needs an owner to raise to) is the real
  decision in this task.
- Move the token to metadata like every other rpc, and give
  lifecycle a public `require` spelling so the handler stops
  touching an underscore.
- Test: bind, let the TTL pass, sweep, ping - assert the error,
  and assert a fresh bind still works.
