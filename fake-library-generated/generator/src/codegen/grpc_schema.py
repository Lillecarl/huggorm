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

from google.protobuf import descriptor_pb2

PKG = "nixmock.v1"
FILE = "nixmock/v1/api.proto"

SCALARS = {"str": "string", "int": "sint64", "bool": "bool"}

HANDLE = "Handle"


def _field(msg, name, number, type_name=None, proto_type=None):
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


def _scalar_const(name):
    t = descriptor_pb2.FieldDescriptorProto()
    return getattr(t, "TYPE_" + name.upper())


# -- naming: the one place the conventions live ---------------------------

def value_msg_name(cls_name: str) -> str:
    return f"{cls_name}Msg"


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
        _msg_arg_type(type_str, kinds)
    except TypeError:
        if type_str in ("dict", "list", "tuple", "set"):
            return (f"{type_str} has no wire representation; the schema has "
                    f"no map or struct type yet")
        return (f"{type_str} is not in the manifest, so it has no wire "
                f"policy (an excluded base class, most likely - see "
                f"tasks/018)")
    return None


def annotate(manifest: dict) -> dict:
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

def _wire_kinds(manifest: dict) -> dict[str, str]:
    """Surface type name -> "value" | "proxy", across both groups."""
    return {
        name: proto["wire"]
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
    }


def _msg_arg_type(type_str, kinds):
    """Surface type string -> (proto_type_const|None, message_name|None)."""
    if type_str == "None":
        return None, None
    if type_str in SCALARS:
        return _scalar_const(SCALARS[type_str]), None
    kind = kinds.get(type_str)
    if kind == "value":
        return None, value_msg_name(type_str)
    if kind == "proxy":
        return None, HANDLE
    raise TypeError(
        f"cannot put {type_str!r} on the wire: it is neither a scalar nor a "
        f"class carrying a _wire policy. Declare _wire on the binding.")


def _add_common(file_dp, manifest):
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
                pt, msg = _msg_arg_type(ftype.removesuffix("?"), kinds)
                _field(m, fname, n, proto_type=pt, type_name=msg)


def _add_service(file_dp, cls_name, proto, kinds):
    svc = file_dp.service.add()
    svc.name = proto["service"]

    if "acquire" in proto:
        req = file_dp.message_type.add()
        req.name = proto["acquire"]["req"]
        for n, param in enumerate(proto["ctor"], start=1):
            pt, msg = _msg_arg_type(param["type"], kinds)
            _field(req, param["name"], n, proto_type=pt, type_name=msg)
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
            pt, msg = _msg_arg_type(p["type"], kinds)
            _field(req, p["name"], n, proto_type=pt, type_name=msg)
            n += 1
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = m["rpc"]["resp"]
        rt, rmsg = _msg_arg_type(m["return_type"], kinds)
        if rt is not None or rmsg is not None:
            _field(resp, "result", 1, proto_type=rt, type_name=rmsg)
        rpc.output_type = f".{PKG}.{resp.name}"


def _add_session(f):
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


def _add_free_service(file_dp, manifest, kinds):
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
            pt, msg = _msg_arg_type(p["type"], kinds)
            _field(req, p["name"], n, proto_type=pt, type_name=msg)
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = proto["rpc"]["resp"]
        rt, rmsg = _msg_arg_type(proto["return_type"], kinds)
        if rt is not None or rmsg is not None:
            _field(resp, "result", 1, proto_type=rt, type_name=rmsg)
        rpc.output_type = f".{PKG}.{resp.name}"


def build_fdset(manifest: dict) -> bytes:
    fds = descriptor_pb2.FileDescriptorSet()
    f = fds.file.add()
    f.name = FILE
    f.package = PKG
    f.syntax = "proto3"
    _add_common(f, manifest)
    _add_session(f)

    kinds = _wire_kinds(manifest)
    # Returned types expose methods through handles as well: their
    # operations run wherever the producing wrapper put them.
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if proto["wrapped"]:
                _add_service(f, cls_name, proto, kinds)
    _add_free_service(f, manifest, kinds)
    return fds.SerializeToString()
