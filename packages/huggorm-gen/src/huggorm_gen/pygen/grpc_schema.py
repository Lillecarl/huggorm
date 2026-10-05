"""
Manifest -> protobuf schema, and the single source of RPC naming.

Builds a FileDescriptorSet from the protocol dicts in the manifest.
Emitted at build time as grpc_schema.pb beside the generated Python - one
artifact, four uses: grpclib dispatch, dynamic message classes on any
client, reflection bytes later, and protoc input for other languages
via a print step.

Conventions:
- package huggorm.v1, single file huggorm/v1/api.proto
- every rpc's request field 1 is `Handle self` - instances live behind
  handles acquired from the Session service
- wire-value types get a real message built from the _wire_fields the
  binding declares; proxy types appear as Handle fields; scalars map
  directly. NO type name is hardcoded here: adding a wire-value means
  editing one declaration and nothing else.

This module also owns naming. annotate() writes every rpc's service,
method path and message names INTO the manifest, so the server and the
client read them instead of each recomputing the convention. A rename
here reaches both sides at build time rather than at first call.
"""

from typing import Any

from google.protobuf import descriptor_pb2

from huggorm_gen import ir
from huggorm_gen.ir import (
    ACQUIRE,
    FREE_SERVICE,
    service_name,
    wire_method,
)
from huggorm_gen.payload.wiretypes import (
    MAP_KEY,
    SCALAR_NAMES,
    arm_field,
    entry_name,
)

Proto = dict[str, Any]

PKG = ir.PROTO_PACKAGE
FILE = "huggorm/v1/api.proto"

# `int` is sint64 and `uint` is uint64, which is the whole of what the
# two widths are for. sint64 zigzags, so a Unix time before the epoch
# costs one byte rather than ten; uint64 is the only proto type that
# holds the top half of a uint64_t at all (huggorm#79).
SCALARS = {"str": "string", "int": "sint64", "uint": "uint64",
           "float": "double", "bool": "bool", "bytes": "bytes"}
assert set(SCALARS) == set(SCALAR_NAMES), "scalar tables disagree"

HANDLE = "Handle"

# The hand-written Session messages' one scalar, and what a log stream
# answers with. `_policy.LOG_RECORDS` is emitted from the second, so
# the codec reads the stream by the type the schema wrote.
INT = ir.TypeRef.named("int", "scalar")
LOG_RECORDS = ir.TypeRef.list_of(ir.TypeRef.named("LogRecord", "value"))


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


def _with_presence(msg: Any, f: Any) -> Any:
    """Give a SCALAR field real presence, proto3's own way.

    A synthetic one-field oneof named `_<field>`, plus the
    proto3_optional flag. That is exactly what `optional string x = 1;`
    compiles to, and it has been in proto3 since protobuf 3.15.

    This is the thing whose absence several refusals used to cite. The
    sentence "proto3 gives a scalar field no presence" described what
    this builder emitted, not what proto3 can express - and the
    difference is one flag and one oneof entry (huggorm#48).

    A message field needs none of it: it has presence already. A
    repeated field can take none of it, and needs none - an absent
    repeated field IS an empty one (huggorm#41).

    Synthetic oneofs must follow every real one in oneof_decl. Nothing
    built through here declares a real oneof; the one message that
    does, NixValue, is hand-built and carries no optional scalar."""
    oneof = msg.oneof_decl.add()
    oneof.name = f"_{f.name}"
    f.oneof_index = len(msg.oneof_decl) - 1
    f.proto3_optional = True
    return f


def _add_map_field(msg: Any, name: str, number: int,
                   value: tuple[int | None, str | None]) -> Any:
    """A `map<string, V>` field, plus the entry message it needs.
    `value` is V's (proto_type, message), as `_leaf` answers."""
    entry = msg.nested_type.add()
    entry.name = entry_name(name)
    entry.options.map_entry = True
    _field(entry, "key", 1, proto_type=_scalar_const(SCALARS[MAP_KEY]))
    vt, vmessage = value
    _field(entry, "value", 2, proto_type=vt, type_name=vmessage)
    f = _field(msg, name, number, type_name=f"{msg.name}.{entry.name}")
    f.label = f.LABEL_REPEATED
    return f


def _add_typed_field(msg: Any, name: str, number: int, t: ir.TypeRef,
                     optional: bool = False) -> Any:
    """One field of a resolved type: the structure decides the field's
    shape and the leaf's kind decides its type.

    `T | None` and `optional=True` mean one thing: the field is built
    for T and given presence if it needs any. A map cannot be one type
    constant: proto3 spells it as a repeated field of an entry message
    the containing type carries, so this builds that message too."""
    if t.optional:
        t, optional = t.required, True
    if t.origin == "dict":
        return _add_map_field(msg, name, number, _leaf(t.args[0]))
    if t.origin == "list":
        pt, message = _leaf(t.args[0])
        f = _field(msg, name, number, proto_type=pt, type_name=message)
        f.label = f.LABEL_REPEATED
        return f
    pt, message = _leaf(t)
    f = _field(msg, name, number, proto_type=pt, type_name=message)
    if optional and pt is not None:
        _with_presence(msg, f)
    return f


def _leaf(t: ir.TypeRef) -> tuple[int | None, str | None]:
    """What one resolved leaf goes in a field as: (proto_type, None)
    for a scalar, (None, message) for everything else."""
    if t.origin:
        # proto3 nests neither container in the other; `ir.wire_blocker`
        # keeps such a call off the wire before it gets here.
        raise TypeError(f"cannot put {t.spelling!r} in one field")
    if (builtin := t.scalar) is not None:
        # `datetime.timedelta` is an int of microseconds: a fact about
        # the WIRE, which `wiretypes.SPELLED` states.
        return _scalar_const(SCALARS[builtin]), None
    if t.kind == "union":
        # A SUM, as protobuf's own tagged union: one message per alias,
        # holding one `oneof`, called after the alias.
        return None, union_msg_name(t.name)
    if t.kind == "enum":
        # A StrEnum member IS a str. The type is for the caller.
        return _scalar_const(SCALARS["str"]), None
    if t.kind == "error":
        # The message the status details carry: one shape for one
        # error class, raised or held in a value.
        return None, fault_msg_name(t.name)
    if t.kind == "value":
        return None, value_msg_name(t.name)
    if t.kind == "proxy":
        return None, HANDLE
    raise TypeError(f"cannot put {t.spelling!r} on the wire: it is a "
                    f"{t.kind}, which has no field type")


def _scalar_const(name: str) -> int:
    # protobuf ships no stubs for its own generated descriptor
    # module, so a typechecker cannot see these names. The shapes are
    # fixed by the protobuf spec.
    t = descriptor_pb2.FieldDescriptorProto()  # type: ignore[attr-defined]
    return int(getattr(t, "TYPE_" + name.upper()))


# -- naming: the one place the conventions live ---------------------------

def union_msg_name(alias: str) -> str:
    """The message one union alias becomes.

    Named after the alias, which is the whole reason a union is
    written as one: `StorePath | DerivedPathBuilt` has no name and a
    message needs one."""
    return f"{alias}Msg"


def value_msg_name(cls_name: str) -> str:
    return f"{cls_name}Msg"


def fault_msg_name(cls_name: str) -> str:
    """The message one declared exception class travels as.

    Its own suffix, not "Msg": an error class and a wire-value class
    could share a name, and two top-level messages in one file cannot."""
    return f"{cls_name}Fault"




# -- schema ---------------------------------------------------------------

FAULT = "Fault"


def _add_faults(file_dp: Any, model: ir.Model) -> None:
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

    for cls_name, error in model.errors.classes.items():
        m = file_dp.message_type.add()
        m.name = fault_msg_name(cls_name)
        for n, field in enumerate(error.wire_fields, start=1):
            _add_typed_field(m, field.name, n, field.type)


def _add_common(file_dp: Any, model: ir.Model) -> None:
    handle = file_dp.message_type.add()
    handle.name = HANDLE
    _field(handle, "id", 1, proto_type=_scalar_const("string"))

    # Wire-value messages, built from the contract each binding declares.
    for c in (*model.constructed, *model.handed_back):
        if c.wire != "value":
            continue
        m = file_dp.message_type.add()
        m.name = value_msg_name(c.name)
        for n, field in enumerate(c.wire_fields, start=1):
            # Presence reaches the schema: the codec once answered an
            # optional by reading an empty string as absent (huggorm#48).
            _add_typed_field(m, field.name, n, field.type)

    # ...and one message per UNION, holding one oneof.
    #
    # This is where gRPC beats both of Nix's own encodings. The daemon
    # sends a DerivedPath as a string and parses it back; the JSON
    # form tags the arms by SHAPE - a string means opaque, `["*"]`
    # means all outputs - and both work only because the alternatives
    # happen not to collide. A oneof is a real tag, and protobuf lets
    # a message hold itself, so the recursive arm needs nothing said
    # about it here (huggorm#59).
    for alias, arms in model.unions.items():
        m = file_dp.message_type.add()
        m.name = union_msg_name(alias)
        one = m.oneof_decl.add()
        one.name = "raw"
        for n, arm in enumerate(arms, start=1):
            # An arm is never `optional`: the oneof IS the presence,
            # and marking a member optional would add a second,
            # disagreeing one.
            f = _add_typed_field(m, arm_field(arm.name), n, arm)
            f.oneof_index = 0


def _add_service(file_dp: Any, model: ir.Model, c: ir.ClassModel,
                 acquirable: bool) -> None:
    svc = file_dp.service.add()
    svc.name = c.service

    if acquirable:
        req = file_dp.message_type.add()
        req.name = c.acquire.req
        for n, param in enumerate(c.ctor, start=1):
            # The client skips a None argument and the server decodes
            # with optional=True, so the field has to be able to say
            # "absent" rather than lean on an empty string.
            _add_typed_field(req, param.name, n, param.type,
                             optional=param.default == "None")
        rpc = svc.method.add()
        rpc.name = ACQUIRE
        rpc.input_type = f".{PKG}.{req.name}"
        rpc.output_type = f".{PKG}.{HANDLE}"

    for m in c.methods:
        if not model.offered(m):
            continue  # no wire representation; the build says why
        names = c.rpc(m)
        rpc = svc.method.add()
        rpc.name = wire_method(m.name)

        req = file_dp.message_type.add()
        req.name = names.req
        _field(req, "self", 1, type_name=HANDLE)
        for n, p in enumerate(m.params, start=2):
            _add_typed_field(req, p.name, n, p.type)
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = names.resp
        if m.returns is not None:
            _add_typed_field(resp, "result", 1, m.returns)
        rpc.output_type = f".{PKG}.{resp.name}"


def _add_session(f: Any) -> None:
    sess = f.service.add()
    sess.name = "Session"

    # Session keeps only what is genuinely protocol: connection identity
    # and handle lifetime. Construction moved onto each class's own
    # service, where it can carry typed arguments.

    # Connection lifecycle (huggorm#2). The connection token travels in
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
    # The digest of the whole schema, which bind compares before it
    # answers (huggorm#22). Its number must never move: it is the one
    # field two different schemas have to agree on.
    _field(bind_req, "schema_digest", 2, proto_type=_scalar_const("string"))
    bnd = sess.method.add()
    bnd.name = "Bind"
    bnd.input_type = f".{PKG}.BindReq"
    bnd.output_type = f".{PKG}.ConnResp"

    # No token field: it rides in the x-huggorm-conn metadata like
    # every other rpc's does. An empty request message is the right
    # shape for a probe whose only question is "am I still bound"
    # (huggorm#49).
    ping_req = f.message_type.add()
    ping_req.name = "PingReq"
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
    # closed (huggorm#28). A garbage collector frees many objects at
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
    _add_log_stream(f, sess)


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


# -- the log stream --------------------------------------------------------


def _options(req: Any, first: int) -> None:
    """The two fields a log subscription carries, wherever it is made.

    Stated once because both requests carry them, and the numbering
    is a parameter because only one of the two has a handle in front.

    `level` gets real presence and `capacity` does not, and the two
    are not the same question. Level 0 is lvlError, which is a
    subscription somebody means - "errors only" - so an unset field
    and a zero one have to differ. Capacity 0 is not a queue at all,
    so zero is free to mean "the binding's own default" (huggorm#48).
    """
    _add_typed_field(req, "capacity", first, INT)
    _add_typed_field(req, "level", first + 1, INT, optional=True)


def _add_log_stream(f: Any, sess: Any) -> None:
    """The one rpc that travels the other way, unsolicited.

    Hand-written, like the rest of Session, and for the reason
    huggorm#32 gives: a log stream is PROTOCOL. Every other rpc came
    out of a binding declaration, because every other rpc is a call
    someone made. This one answers records nobody asked for one at a
    time, so there is no method for it to be the wire form of.

    Three things here are decisions rather than shape.

    **It streams.** `server_streaming` is the first use of it in this
    schema, and it is what the descriptor has to SAY - reflection and
    grpcurl read the flag, and a descriptor that calls this unary
    while dispatch streams is a schema that lies.

    **A message is a DRAIN, not a record.** `LogStream.drain` answers
    everything waiting in one call, and a batch per drain keeps that
    shape rather than fanning one drain into forty messages.

    **`dropped` rides with every batch.** The queue is bounded, so it
    refuses a message when it is full - and a drop the client cannot
    see is this repo's named failure mode. The count is cumulative, so
    a client that missed a batch still learns the total.

    TWO rpcs now, one response message. `Logs` names a state and
    subscribes on its thread; `ProcessLogs` names nothing and takes
    what no subscribed thread claimed. A batch of records and a drop
    count is the whole answer either way, so a second response
    message would be the same fact declared twice.

    Where the options live is `_options`, for the same reason."""
    req = f.message_type.add()
    req.name = "LogsReq"
    # Which state's thread to subscribe on. The tap routes by thread,
    # and an EvalState owns one, so the handle names the subscription.
    _field(req, "state", 1, type_name=HANDLE)
    _options(req, 2)

    # The same subscription with nothing to name. The process-wide
    # sink takes records no subscribed thread claimed, so there is no
    # handle to address and the message is the options alone
    # (huggorm#85).
    #
    # Numbered from 1 rather than leaving a hole where the handle
    # would be. They are two messages, not one message with a field
    # switched off, and a reserved gap would say the opposite.
    process = f.message_type.add()
    process.name = "ProcessLogsReq"
    _options(process, 1)

    # ONE response message for both. A batch of records and a drop
    # count is the whole answer either way, and a second message with
    # the same two fields would be the same fact declared twice.
    resp = f.message_type.add()
    resp.name = "LogsResp"
    _add_typed_field(resp, "records", 1, LOG_RECORDS)
    _add_typed_field(resp, "dropped", 2, INT)

    for name, input_name in (("Logs", "LogsReq"),
                             ("ProcessLogs", "ProcessLogsReq")):
        rpc = sess.method.add()
        rpc.name = name
        rpc.input_type = f".{PKG}.{input_name}"
        rpc.output_type = f".{PKG}.LogsResp"
        rpc.server_streaming = True

    # The end of a state's records so far. A call on the state's own
    # thread answers its request id, and its "finalized" marker lands
    # in that thread's queue after every record raised before it. A
    # reader that sees the marker holds everything; a timed window
    # would drop what came late.
    barrier = f.message_type.add()
    barrier.name = "LogsBarrierReq"
    _field(barrier, "state", 1, type_name=HANDLE)
    barrier_resp = f.message_type.add()
    barrier_resp.name = "LogsBarrierResp"
    _add_typed_field(barrier_resp, "request", 1, INT)
    rpc = sess.method.add()
    rpc.name = "LogsBarrier"
    rpc.input_type = f".{PKG}.LogsBarrierReq"
    rpc.output_type = f".{PKG}.LogsBarrierResp"


def _add_free_service(file_dp: Any, model: ir.Model) -> None:
    """One service for every free function the wire can represent."""
    wired = [model.functions[n] for n in sorted(model.functions)
             if not model.function_blockers(model.functions[n])]
    if not wired:
        return
    svc = file_dp.service.add()
    # Through service_name, like every other service: method_path()
    # appends "Service", so the bare name would leave the descriptor
    # calling it Functions while dispatch routed FunctionsService.
    svc.name = service_name(FREE_SERVICE)
    for fn in wired:
        rpc = svc.method.add()
        rpc.name = fn.name

        req = file_dp.message_type.add()
        req.name = fn.rpc.req
        # No `self` field: there is no instance to address.
        for n, p in enumerate(fn.params, start=1):
            _add_typed_field(req, p.name, n, p.type)
        rpc.input_type = f".{PKG}.{req.name}"

        resp = file_dp.message_type.add()
        resp.name = fn.rpc.resp
        if fn.returns is not None:
            _add_typed_field(resp, "result", 1, fn.returns)
        rpc.output_type = f".{PKG}.{resp.name}"


def build_fdset(model: ir.Model) -> bytes:
    fds = descriptor_pb2.FileDescriptorSet()  # type: ignore[attr-defined]
    f = fds.file.add()
    f.name = FILE
    f.package = PKG
    f.syntax = "proto3"
    _add_common(f, model)
    _add_faults(f, model)
    _add_session(f)
    # Returned types expose methods through handles as well: their
    # operations run wherever the producing wrapper put them.
    acquirable = {c.name for c in model.acquirable}
    for c in (*model.constructed, *model.handed_back):
        if c.served:
            _add_service(f, model, c, c.name in acquirable)
    _add_free_service(f, model)
    return bytes(fds.SerializeToString())
