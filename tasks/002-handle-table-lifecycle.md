# Handle table lifecycle (leases, ownership, reaping)

Review findings 2 + 13. server.py handle table grows monotonically:
proxy returns insert, only explicit Release removes, exceptions and
disconnects leak; no per-connection ownership or cancellation.

Fix sketch: Session object per connection owning its handle namespace;
refcount or lease handles; reap on disconnect. RemoteObj.aclose()
should Release implicitly. Prerequisite for the SSH/stdio/multiproc
transports (014).
