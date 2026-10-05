"""
Manifest -> the typed surface, and the single source of surface naming.

grpc_schema owns the names the WIRE uses. This module owns the names
the PYTHON surface uses: the protocol a class promises, the in-process
class that implements it, and the RPC class that implements it
somewhere else. annotate() stamps them into the manifest so the
emitter, the client and the gates all read one answer instead of each
rebuilding the same convention.

It also decides which methods a protocol may carry at all.

The two implementations agree on everything except proxies. A scalar
is a scalar and a wire-value is the same binding object on both sides
- that is what huggorm#25 bought by not wrapping them. A proxy differs:
in process it is a wrapper holding a runner, remotely it is a class
holding a handle.

For a RETURN that costs nothing. The protocol names the protocol, both
implementations return something that satisfies it, and return types
are covariant.

For a PARAMETER it is fatal. Parameters are contravariant, so an
implementation must accept everything the protocol promises. Neither
one can: the in-process wrapper needs a real local object to unwrap,
and the remote class needs a handle the server knows. No single type
describes both, so the method stays off the protocol and this module
records why - the same way wire_blocker reports a type the schema
cannot carry, rather than pretending.
"""

from typing import Any

Proto = dict[str, Any]

PROTOCOL_MODULE = "protocols"
RPC_MODULE = "rpc"

# Every implementation closes the same way, and only the meaning
# differs: in process it shuts the runner's thread down, remotely it
# gives the lease back. So it belongs on the protocol, and it is the
# one method no binding declares.
ACLOSE = "aclose"

# The registry a client uses to turn a handle into an object of the
# right class.
REGISTRY = "RPC_CLASSES"


def protocol_name(cls_name: str) -> str:
    return f"{cls_name}Like"


def async_class_name(cls_name: str) -> str:
    return f"Async{cls_name}"


def rpc_class_name(cls_name: str) -> str:
    return f"RPC{cls_name}"
