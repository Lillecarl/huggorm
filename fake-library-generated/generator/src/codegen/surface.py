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
- that is what tasks/025 bought by not wrapping them. A proxy differs:
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


def wrapped_names(manifest: dict) -> set[str]:
    """Every class the codegen wraps, across both groups."""
    return {
        name
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
        if proto["wrapped"]
    }


def protocol_blockers(method: dict, wrapped: set[str]) -> list[str]:
    """Why this method cannot appear on the protocol, or [] if it can."""
    return [
        f"parameter {p['name']!r}: {p['type']} travels as a proxy, so the "
        f"in-process surface takes a local wrapper and the remote surface "
        f"takes a handle. A protocol parameter is contravariant, so no "
        f"single type describes both."
        for p in method["params"]
        if p["type"] in wrapped
    ]


def order(manifest: dict) -> list[dict]:
    """The wrapped protocol dicts, bases before subclasses.

    Only class inheritance needs the order - annotations are lazy in
    both emitted modules - but a subclass whose base is not defined yet
    is a NameError at import, so it is not optional."""
    protos = [
        proto
        for group in ("returned_types", "wrappers")
        for proto in manifest[group].values()
        if proto["wrapped"]
    ]
    by_name = {p["name"]: p for p in protos}
    out, placed = [], set()

    def place(proto):
        if proto["name"] in placed:
            return
        placed.add(proto["name"])
        base = proto.get("async_base")
        if base in by_name:
            place(by_name[base])
        out.append(proto)

    for proto in protos:
        place(proto)
    return out


def annotate(manifest: dict) -> dict:
    """Stamp the surface names onto the manifest, in place."""
    wrapped = wrapped_names(manifest)
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if not proto["wrapped"]:
                continue
            proto["protocol"] = protocol_name(cls_name)
            proto["async_class"] = async_class_name(cls_name)
            proto["rpc_class"] = rpc_class_name(cls_name)
            for m in proto["methods"]:
                m["protocol_blockers"] = protocol_blockers(m, wrapped)
    return manifest


def check_adoptable(manifest: dict, adoptable: set[str]) -> list[str]:
    """Every wrapped return type must be one the emitter can adopt.

    The in-process wrapper hands a produced object to Async<T>(obj,
    runner), which only a returned type has. A method returning a
    wrapped class that is CONSTRUCTED instead would emit a call to a
    constructor of a different shape - working code, wrong object.
    Nothing does this today; the check is here so nothing starts to."""
    wrapped = wrapped_names(manifest)
    return [
        f"{proto['name']}.{m['name']} returns {m['return_type']}, which is "
        f"wrapped but not adoptable: only a returned type has the "
        f"(obj, runner) constructor the emitter would call."
        for group in ("wrappers", "returned_types")
        for proto in manifest[group].values()
        for m in proto["methods"]
        if m["return_type"] in wrapped and m["return_type"] not in adoptable
    ]
