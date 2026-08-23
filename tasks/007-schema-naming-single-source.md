# Schema naming: single source of truth

Review finding 7. _resp() exists in three copies (grpc_schema.py,
server.py, remote.py); a rename breaks dispatch only at first RPC.
StrMsg/Int64Msg/BoolMsg are declared but never referenced; server
decode's scalar branch applies message-shaped conversion to plain
scalars (works by coincidence).

Fix: grpc_schema owns all naming and the type->message map; server and
client import it; delete unused messages.
