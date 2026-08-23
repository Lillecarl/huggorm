"""
Manifest -> protobuf schema.

Builds a FileDescriptorSet from the protocol dicts in the manifest.
Emitted at build time as grpc_schema.pb next to manifest.json - one
artifact, four uses: grpclib dispatch, dynamic message classes on any
client, reflection bytes later, and protoc input for other languages
via a print step.

Conventions:
- package nixmock.v1, single file nixmock/v1/api.proto
- every rpc's request field 1 is `Handle self` - instances live behind
  handles acquired from the Session service
- wire-value types get real messages (StorePathMsg, DerivedPathMsg);
  proxy types appear as Handle fields; scalars map directly.
"""

from google.protobuf import descriptor_pb2

PKG = "nixmock.v1"
FILE = "nixmock/v1/api.proto"

SCALARS = {"str": "string", "int": "sint64", "bool": "bool"}


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


def _msg_arg_type(type_str):
    """Surface type string -> (proto_type_const|None, message_name|None)."""
    if type_str == "None":
        return None, None
    if type_str in SCALARS:
        return _scalar_const(SCALARS[type_str]), None
    if type_str in ("StorePath", "DerivedPath"):
        return None, type_str + "Msg"
    # proxy handles: Value, Derivation, stores passed as args someday
    return None, "Handle"


def _add_common(file_dp):
    simple = {
        "Handle": [("id", 1, "string")],
        "StrMsg": [("v", 1, "string")],
        "BoolMsg": [("v", 1, "bool")],
        "Int64Msg": [("v", 1, "sint64")],
        "StorePathMsg": [("base_name", 1, "string")],
    }
    for name, fields in simple.items():
        m = file_dp.message_type.add()
        m.name = name
        for fname, num, ptype in fields:
            _field(m, fname, num, proto_type=_scalar_const(ptype))

    derived = file_dp.message_type.add()
    derived.name = "DerivedPathMsg"
    f = _field(derived, "path", 1, type_name="StorePathMsg")
    _field(derived, "output", 2, proto_type=_scalar_const("string"))


def _resp(cls_name, m):
    # Class-prefixed: LocalStore and RemoteStore share method names, and
    # top-level message names must be unique across the file.
    return f"{cls_name}_{m['name'].title().replace('_', '')}Resp"


def _add_service(file_dp, cls_name, proto):
    svc = file_dp.service.add()
    svc.name = cls_name + "Service"
    for m in proto["methods"]:
        rpc = svc.method.add()
        rpc.name = m["name"]

        req = file_dp.message_type.add()
        req.name = f"{cls_name}_{m['name']}Req"
        _field(req, "self", 1, type_name="Handle")
        n = 2
        for p in m["params"]:
            pt, msg = _msg_arg_type(p["type"])
            _field(req, p["name"], n, proto_type=pt, type_name=msg)
            n += 1
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = _resp(cls_name, m)
        rt, rmsg = _msg_arg_type(m["return_type"])
        if rt is not None or rmsg is not None:
            _field(resp, "result", 1, proto_type=rt, type_name=rmsg)
        rpc.output_type = f".{PKG}.{resp.name}"


def build_fdset(manifest: dict) -> bytes:
    fds = descriptor_pb2.FileDescriptorSet()
    f = fds.file.add()
    f.name = FILE
    f.package = PKG
    f.syntax = "proto3"
    _add_common(f)

    sess = f.service.add()
    sess.name = "Session"
    acquire_req = f.message_type.add()
    acquire_req.name = "AcquireReq"
    _field(acquire_req, "class", 1, proto_type=_scalar_const("string"))
    acq = sess.method.add()
    acq.name = "Acquire"
    acq.input_type = f".{PKG}.AcquireReq"
    acq.output_type = f".{PKG}.Handle"
    rel = sess.method.add()
    rel.name = "Release"
    rel.input_type = f".{PKG}.Handle"
    rel.output_type = f".{PKG}.Handle"

    for cls_name, proto in manifest["wrappers"].items():
        _add_service(f, cls_name, proto)
    # Returned types expose methods through handles as well: their
    # operations run wherever the producing wrapper put them.
    for cls_name, proto in manifest["returned_types"].items():
        _add_service(f, cls_name, proto)
    return fds.SerializeToString()
