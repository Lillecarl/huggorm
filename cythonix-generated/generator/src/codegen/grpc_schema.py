"""
Manifest -> protobuf schema, and the single source of RPC naming.

Builds a FileDescriptorSet from the protocol dicts in the manifest.
Emitted at build time as grpc_schema.pb next to manifest.json - one
artifact, four uses: grpclib dispatch, dynamic message classes on any
client, reflection bytes later, and protoc input for other languages
via a print step.

Conventions:
- package nixmock.v1, single file nixmock/v1/api.proto
- every rpc's request field 1 is `Handle self` - instances live behind
  handles acquired from the Session service
- wire-value types get a real message built from the _wire_fields the
  binding declares; proxy types appear as Handle fields; scalars map
  directly. NO type name is hardcoded here: adding a wire-value means
  editing the pyx and nothing else.

This module also owns naming. annotate() writes every rpc's service,
method path and message names INTO the manifest, so the server and the
client read them instead of each recomputing the convention. A rename
here reaches both sides at build time rather than at first call.
"""

from typing import Any

from google.protobuf import descriptor_pb2

from codegen.wiretypes import (
    MAP_KEY,
    SCALAR_NAMES,
    entry_name,
    head,
    list_value,
    map_value,
)

Proto = dict[str, Any]

PKG = "nixmock.v1"
FILE = "nixmock/v1/api.proto"

SCALARS = {"str": "string", "int": "sint64", "bool": "bool",
           "bytes": "bytes"}
assert set(SCALARS) == set(SCALAR_NAMES), "scalar tables disagree"

HANDLE = "Handle"


def _field(msg: Any, name: str, number: int, type_name: str | None = None,
           proto_type: int | None = None) -> Any:
    """Append one field to a DescriptorProto message."""
    f = msg.field.add()
    f.name, f.number = name, number
    f.label = f.LABEL_OPTIONAL
    if proto_type is not None:
        f.type = proto_type
    else:
        f.type = f.TYPE_MESSAGE
        f.type_name = f".{PKG}.{type_name}"
    return f


def _add_field(msg: Any, name: str, number: int, type_str: str,
               kinds: dict[str, str]) -> Any:
    """Append one field of the declared surface type.

    Three shapes, and only the first is a plain lookup. A list is a
    repeated field of its element type. A map cannot be described by a
    type constant at all: proto3 spells it as a repeated field of a
    message the containing type carries, so this builds that message
    too."""
    if (value_type := map_value(type_str)) is not None:
        return _add_map_field(msg, name, number, value_type, kinds)
    if (item_type := list_value(type_str)) is not None:
        pt, message = _msg_arg_type(item_type, kinds)
        f = _field(msg, name, number, proto_type=pt, type_name=message)
        f.label = f.LABEL_REPEATED
        return f
    pt, message = _msg_arg_type(type_str, kinds)
    return _field(msg, name, number, proto_type=pt, type_name=message)


def _add_map_field(msg: Any, name: str, number: int, value_type: str,
                   kinds: dict[str, str]) -> Any:
    """A `map<string, V>` field, plus the entry message it needs."""
    entry = msg.nested_type.add()
    entry.name = entry_name(name)
    entry.options.map_entry = True
    _field(entry, "key", 1, proto_type=_scalar_const(SCALARS[MAP_KEY]))
    vt, vmessage = _msg_arg_type(value_type, kinds)
    _field(entry, "value", 2, proto_type=vt, type_name=vmessage)
    f = _field(msg, name, number, type_name=f"{msg.name}.{entry.name}")
    f.label = f.LABEL_REPEATED
    return f


def _scalar_const(name: str) -> int:
    # protobuf ships no stubs for its own generated descriptor
    # module, so a typechecker cannot see these names. The shapes are
    # fixed by the protobuf spec.
    t = descriptor_pb2.FieldDescriptorProto()  # type: ignore[attr-defined]
    return int(getattr(t, "TYPE_" + name.upper()))


# -- naming: the one place the conventions live ---------------------------

def value_msg_name(cls_name: str) -> str:
    return f"{cls_name}Msg"


def fault_msg_name(cls_name: str) -> str:
    """The message one declared exception class travels as.

    Its own suffix, not "Msg": an error class and a wire-value class
    could share a name, and two top-level messages in one file cannot."""
    return f"{cls_name}Fault"


def service_name(cls_name: str) -> str:
    return f"{cls_name}Service"


def req_name(cls_name: str, method: str) -> str:
    return f"{cls_name}_{method}Req"


def resp_name(cls_name: str, method: str) -> str:
    # Class-prefixed: LocalStore and RemoteStore share method names, and
    # top-level message names must be unique across the file.
    return f"{cls_name}_{method.title().replace('_', '')}Resp"


def method_path(cls_name: str, method: str) -> str:
    return f"/{PKG}.{service_name(cls_name)}/{method}"


# Construction is an rpc on the class's OWN service, not a string-keyed
# call on Session. Session/Acquire took a class name and no arguments,
# so it could only ever build things whose constructor takes nothing -
# and it type-checked neither the name nor the absent arguments.
ACQUIRE = "Acquire"

# Free functions have no instance, so they cannot hang off a class's
# service. They share one.
FREE_SERVICE = "Functions"


def wire_blocker(type_str: str, kinds: dict[str, str]) -> str | None:
    """Why this type cannot cross the wire, or None if it can.

    Reported rather than raised, so a function that is unrepresentable
    today still gets its in-process wrapper and the build says exactly
    what is missing."""
    try:
        value_type = map_value(type_str)
        item_type = list_value(type_str)
    except TypeError as e:
        return str(e)
    # A container of PROXIES stays refused, whichever container it is.
    # The element type is what actually goes in the field, so from here
    # on it is the type under test.
    for element in (value_type, item_type):
        if element is None:
            continue
        if kinds.get(element) == "proxy":
            return (f"{type_str}: a container of proxies would grant one "
                    f"lease per element, and nothing grants leases in bulk "
                    f"(tasks/031)")
        type_str = element
    try:
        _msg_arg_type(type_str, kinds)
    except TypeError:
        if head(type_str) in ("tuple", "set", "frozenset"):
            return (f"{type_str} has no wire representation; a protobuf "
                    f"field is a scalar, a message, a map or a repeated one")
        return (f"{type_str} is not in the manifest, so it has no wire "
                f"policy (an excluded base class, most likely - see "
                f"tasks/018)")
    return None


def annotate(manifest: Proto) -> Proto:
    """Stamp the wire names onto the manifest, in place.

    Every consumer previously re-derived them from the same convention
    kept in three copies; a rename broke dispatch at the first rpc call
    rather than at build time."""
    manifest["package"] = PKG
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if proto["wire"] == "value":
                proto["message"] = value_msg_name(cls_name)
            # An unwrapped class has no remote surface: it crosses as a
            # value, so a caller already holds the object and calls it
            # directly. Giving it a service would publish rpcs that
            # nobody can reach a handle for.
            if not proto["wrapped"]:
                continue
            proto["service"] = service_name(cls_name)
            if group == "wrappers":
                proto["acquire"] = {
                    "path": method_path(cls_name, ACQUIRE),
                    "req": req_name(cls_name, ACQUIRE),
                }
            for m in proto["methods"]:
                m["rpc"] = {
                    "path": method_path(cls_name, m["name"]),
                    "req": req_name(cls_name, m["name"]),
                    "resp": resp_name(cls_name, m["name"]),
                }

    kinds = _wire_kinds(manifest)
    for fname, proto in manifest.get("free_functions", {}).items():
        if not proto["wrapped"]:
            # No policy, so no wrapper and nothing to call remotely.
            # It is in the manifest to describe the module, not to be
            # published.
            proto["wire_blockers"] = [
                "no threading policy, so the function has no async form "
                "for a server to call"]
            continue
        blockers = [
            f"parameter {p['name']!r}: {why}"
            for p in proto["params"]
            if (why := wire_blocker(p["type"], kinds))
        ]
        if (why := wire_blocker(proto["return_type"], kinds)):
            blockers.append(f"return type: {why}")
        proto["wire_blockers"] = blockers
        if not blockers:
            proto["rpc"] = {
                "path": method_path(FREE_SERVICE, fname),
                "req": req_name(FREE_SERVICE, fname),
                "resp": resp_name(FREE_SERVICE, fname),
            }
    return manifest


# -- schema ---------------------------------------------------------------

ENUM = "enum"


def _wire_kinds(manifest: Proto) -> dict[str, str]:
    """Surface type name -> "value" | "proxy" | "enum".

    Every declared name and what it is on the wire. A string enum is
    here because it is a declared NAME that is not a class the wire
    knows - and it needs no policy of its own, because a StrEnum
    member is a str and crosses as one."""
    out = {
        name: proto["wire"]
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
    }
    out.update({name: ENUM for name in manifest.get("enums", {})})
    return out


def _msg_arg_type(type_str: str,
                  kinds: dict[str, str]) -> tuple[int | None, str | None]:
    """Surface type string -> (proto_type_const|None, message_name|None)."""
    if type_str == "None":
        return None, None
    if type_str in SCALARS:
        return _scalar_const(SCALARS[type_str]), None
    kind = kinds.get(type_str)
    if kind == ENUM:
        # A StrEnum member IS a str. Nothing about the transport
        # changes; the type exists for the caller, not for the wire.
        return _scalar_const(SCALARS["str"]), None
    if kind == "value":
        return None, value_msg_name(type_str)
    if kind == "proxy":
        return None, HANDLE
    raise TypeError(
        f"cannot put {type_str!r} on the wire: it is neither a scalar nor a "
        f"class carrying a _wire policy. Declare _wire on the binding.")


FAULT = "Fault"


def _add_faults(file_dp: Any, manifest: Proto) -> None:
    """How a failure describes itself, in the status details.

    A failed call carries no response message - only a status - so this
    is the only place a typed answer can go. gRPC's own channel for it
    is `grpc-status-details-bin`: a google.rpc.Status whose `details`
    is a repeated Any. So the fault travels as messages, the way
    everything else here does.

    Two of them. `Fault` is what the wrapper always said - a code, a
    message, and the cause approximated by name - and every peer
    understands it. The cause ALSO rides as a message of its own class
    when the bindings declare that class, and then the Any's type name
    IS the identity: the far side resolves it in the schema pool or it
    does not resolve at all. Nothing has to trust a class name, because
    no class name crosses on its own."""
    fault = file_dp.message_type.add()
    fault.name = FAULT
    _field(fault, "code", 1, proto_type=_scalar_const("string"))
    _field(fault, "message", 2, proto_type=_scalar_const("string"))
    # The approximation, kept: a peer that cannot resolve the typed
    # detail still learns what failed and what it said.
    _field(fault, "cause_type", 3, proto_type=_scalar_const("string"))
    _field(fault, "cause_message", 4, proto_type=_scalar_const("string"))

    errors: Proto = manifest.get("errors") or {}
    kinds = _wire_kinds(manifest)
    for cls_name, proto in errors.get("classes", {}).items():
        m = file_dp.message_type.add()
        m.name = fault_msg_name(cls_name)
        for n, (fname, ftype) in enumerate(proto["wire_fields"], start=1):
            _add_field(m, fname, n, ftype.removesuffix("?"), kinds)


def _add_common(file_dp: Any, manifest: Proto) -> None:
    handle = file_dp.message_type.add()
    handle.name = HANDLE
    _field(handle, "id", 1, proto_type=_scalar_const("string"))

    # Wire-value messages, built from the contract each binding declares.
    kinds = _wire_kinds(manifest)
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if proto["wire"] != "value":
                continue
            m = file_dp.message_type.add()
            m.name = value_msg_name(cls_name)
            for n, (fname, ftype) in enumerate(proto["wire_fields"], start=1):
                # Optionality is a codec concern, not a proto3 one.
                _add_field(m, fname, n, ftype.removesuffix("?"), kinds)


def _add_service(file_dp: Any, cls_name: str, proto: Proto,
                 kinds: dict[str, str]) -> None:
    svc = file_dp.service.add()
    svc.name = proto["service"]

    if "acquire" in proto:
        req = file_dp.message_type.add()
        req.name = proto["acquire"]["req"]
        for n, param in enumerate(proto["ctor"], start=1):
            _add_field(req, param["name"], n, param["type"], kinds)
        rpc = svc.method.add()
        rpc.name = ACQUIRE
        rpc.input_type = f".{PKG}.{req.name}"
        rpc.output_type = f".{PKG}.{HANDLE}"

    for m in proto["methods"]:
        rpc = svc.method.add()
        rpc.name = m["name"]

        req = file_dp.message_type.add()
        req.name = m["rpc"]["req"]
        _field(req, "self", 1, type_name=HANDLE)
        n = 2
        for p in m["params"]:
            _add_field(req, p["name"], n, p["type"], kinds)
            n += 1
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = m["rpc"]["resp"]
        if m["return_type"] != "None":
            _add_field(resp, "result", 1, m["return_type"], kinds)
        rpc.output_type = f".{PKG}.{resp.name}"


def _add_session(f: Any) -> None:
    sess = f.service.add()
    sess.name = "Session"

    # Session keeps only what is genuinely protocol: connection identity
    # and handle lifetime. Construction moved onto each class's own
    # service, where it can carry typed arguments.

    # Connection lifecycle (tasks/002). The connection token travels in
    # gRPC metadata on every request; these rpcs manage it.
    conn_resp = f.message_type.add()
    conn_resp.name = "ConnResp"
    _field(conn_resp, "token", 1, proto_type=_scalar_const("string"))
    _field(conn_resp, "lease_ttl", 2, proto_type=_scalar_const("double"))
    ack = f.message_type.add()
    ack.name = "AckResp"
    _field(ack, "ok", 1, proto_type=_scalar_const("bool"))
    bind_req = f.message_type.add()
    bind_req.name = "BindReq"
    _field(bind_req, "claim_token", 1, proto_type=_scalar_const("string"))
    bnd = sess.method.add()
    bnd.name = "Bind"
    bnd.input_type = f".{PKG}.BindReq"
    bnd.output_type = f".{PKG}.ConnResp"

    ping_req = f.message_type.add()
    ping_req.name = "PingReq"
    _field(ping_req, "token", 1, proto_type=_scalar_const("string"))
    png = sess.method.add()
    png.name = "Ping"
    png.input_type = f".{PKG}.PingReq"
    png.output_type = f".{PKG}.AckResp"

    share_req = f.message_type.add()
    share_req.name = "ShareReq"
    _field(share_req, "handle", 1, type_name=HANDLE)
    _field(share_req, "to_token", 2, proto_type=_scalar_const("string"))
    _field(share_req, "mode", 3, proto_type=_scalar_const("string"))
    shr = sess.method.add()
    shr.name = "Share"
    shr.input_type = f".{PKG}.ShareReq"
    shr.output_type = f".{PKG}.AckResp"

    detach_req = f.message_type.add()
    detach_req.name = "DetachReq"
    _field(detach_req, "target", 1, type_name=HANDLE)
    _field(detach_req, "all", 2, proto_type=_scalar_const("bool"))
    det = sess.method.add()
    det.name = "Detach"
    det.input_type = f".{PKG}.DetachReq"
    det.output_type = f".{PKG}.AckResp"

    rel = sess.method.add()
    rel.name = "Release"
    rel.input_type = f".{PKG}.{HANDLE}"
    rel.output_type = f".{PKG}.{HANDLE}"

    # Batched release, for handles the client dropped rather than
    # closed (tasks/028). A garbage collector frees many objects at
    # once, so one rpc per flush beats one per handle. Failures are
    # per-handle: a client cannot know whether a queued id was already
    # released by something else, and one stale id must not sink the
    # rest of the batch.
    many_req = f.message_type.add()
    many_req.name = "ReleaseManyReq"
    field = _field(many_req, "handles", 1, type_name=HANDLE)
    field.label = field.LABEL_REPEATED
    many_resp = f.message_type.add()
    many_resp.name = "ReleaseManyResp"
    _field(many_resp, "released", 1, proto_type=_scalar_const("sint64"))
    _field(many_resp, "unknown", 2, proto_type=_scalar_const("sint64"))
    relm = sess.method.add()
    relm.name = "ReleaseMany"
    relm.input_type = f".{PKG}.ReleaseManyReq"
    relm.output_type = f".{PKG}.ReleaseManyResp"

    _add_value_tree(f, sess)


# -- the recursive value message ------------------------------------------

VALUE = "NixValue"


def _add_value_tree(f: Any, sess: Any) -> None:
    """A value that holds values, and the rpc that fetches one.

    Hand-written, like the rest of Session. This message cannot come
    out of a _wire_fields declaration the way StorePath's does: it is
    recursive, and its arms are the wire KINDS themselves rather than a
    list of typed fields. The generator stays unaware of it; what it
    does know - which class is a tree and how to walk one - reaches the
    server through the manifest, from a declaration next to the
    binding.

    The proxy arm is where laziness lives. A thunk cannot be
    serialized, so it crosses as a handle and the caller forces it with
    another call. The same arm carries every node the walk stopped at,
    so a bounded answer and a lazy one have one shape."""
    proxy = f.message_type.add()
    proxy.name = "NixProxy"
    _field(proxy, "handle", 1, type_name=HANDLE)
    # The handle alone does not say what it is, and no layer above the
    # bindings may name a class. The walk knows, so it says.
    _field(proxy, "cls", 2, proto_type=_scalar_const("string"))

    lst = f.message_type.add()
    lst.name = "NixList"
    field = _field(lst, "items", 1, type_name=VALUE)
    field.label = field.LABEL_REPEATED

    attrs = f.message_type.add()
    attrs.name = "NixAttrs"
    entry = attrs.nested_type.add()
    entry.name = entry_name("entries")
    entry.options.map_entry = True
    _field(entry, "key", 1, proto_type=_scalar_const(SCALARS[MAP_KEY]))
    _field(entry, "value", 2, type_name=VALUE)
    field = _field(attrs, "entries", 1, type_name=f"{attrs.name}.{entry.name}")
    field.label = field.LABEL_REPEATED

    value = f.message_type.add()
    value.name = VALUE
    arm = value.oneof_decl.add()
    arm.name = "v"
    for n, (fname, ptype, msg) in enumerate([
        ("s", "string", None),
        ("i", "sint64", None),
        ("b", "bool", None),
        ("f", "double", None),
        ("proxy", None, proxy.name),
        ("list", None, lst.name),
        ("attrs", None, attrs.name),
    ], start=1):
        field = _field(value, fname, n,
                       proto_type=_scalar_const(ptype) if ptype else None,
                       type_name=msg)
        field.oneof_index = 0

    req = f.message_type.add()
    req.name = "RealizeReq"
    _field(req, "handle", 1, type_name=HANDLE)
    # Two bounds, because a tree is unbounded in two directions and
    # they are not the same problem. depth counts levels EXPANDED, so 1
    # is the root alone; budget is the hard stop, because a single
    # attribute set can hold a hundred thousand entries one level down.
    # Every node the walk stops at costs the caller a lease, which is
    # why both default small.
    _field(req, "depth", 2, proto_type=_scalar_const("sint64"))
    _field(req, "budget", 3, proto_type=_scalar_const("sint64"))

    resp = f.message_type.add()
    resp.name = "RealizeResp"
    _field(resp, "root", 1, type_name=VALUE)
    _field(resp, "nodes", 2, proto_type=_scalar_const("sint64"))
    _field(resp, "truncated", 3, proto_type=_scalar_const("bool"))

    rlz = sess.method.add()
    rlz.name = "Realize"
    rlz.input_type = f".{PKG}.RealizeReq"
    rlz.output_type = f".{PKG}.RealizeResp"


def _add_free_service(file_dp: Any, manifest: Proto,
                      kinds: dict[str, str]) -> None:
    """One service for every free function the wire can represent."""
    wired = {n: p for n, p in manifest.get("free_functions", {}).items()
             if "rpc" in p}
    if not wired:
        return
    svc = file_dp.service.add()
    # Through service_name, like every other service: method_path()
    # already appends "Service", so declaring the bare name here left
    # the descriptor calling it Functions while dispatch routed
    # FunctionsService. Reflection would list it and no call could
    # reach it.
    svc.name = service_name(FREE_SERVICE)
    for fname, proto in wired.items():
        rpc = svc.method.add()
        rpc.name = fname

        req = file_dp.message_type.add()
        req.name = proto["rpc"]["req"]
        # No `self` field: there is no instance to address.
        for n, p in enumerate(proto["params"], start=1):
            _add_field(req, p["name"], n, p["type"], kinds)
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = proto["rpc"]["resp"]
        if proto["return_type"] != "None":
            _add_field(resp, "result", 1, proto["return_type"], kinds)
        rpc.output_type = f".{PKG}.{resp.name}"


def build_fdset(manifest: Proto) -> bytes:
    fds = descriptor_pb2.FileDescriptorSet()  # type: ignore[attr-defined]
    f = fds.file.add()
    f.name = FILE
    f.package = PKG
    f.syntax = "proto3"
    _add_common(f, manifest)
    _add_faults(f, manifest)
    _add_session(f)

    kinds = _wire_kinds(manifest)
    # Returned types expose methods through handles as well: their
    # operations run wherever the producing wrapper put them.
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if proto["wrapped"]:
                _add_service(f, cls_name, proto, kinds)
    _add_free_service(f, manifest, kinds)
    return bytes(fds.SerializeToString())
