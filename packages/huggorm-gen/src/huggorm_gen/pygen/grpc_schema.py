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

from huggorm_gen.payload.wiretypes import (
    CONTAINERS,
    MAP_KEY,
    SCALAR_NAMES,
    arm_field,
    entry_name,
    head,
    list_value,
    map_value,
    optional_value,
    scalar_spelling,
)

Proto = dict[str, Any]

PKG = "huggorm.v1"
FILE = "huggorm/v1/api.proto"

# `int` is sint64 and `uint` is uint64, which is the whole of what the
# two widths are for. sint64 zigzags, so a Unix time before the epoch
# costs one byte rather than ten; uint64 is the only proto type that
# holds the top half of a uint64_t at all (tasks/079).
SCALARS = {"str": "string", "int": "sint64", "uint": "uint64",
           "bool": "bool", "bytes": "bytes"}
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


def _with_presence(msg: Any, f: Any) -> Any:
    """Give a SCALAR field real presence, proto3's own way.

    A synthetic one-field oneof named `_<field>`, plus the
    proto3_optional flag. That is exactly what `optional string x = 1;`
    compiles to, and it has been in proto3 since protobuf 3.15.

    This is the thing whose absence several refusals used to cite. The
    sentence "proto3 gives a scalar field no presence" described what
    this builder emitted, not what proto3 can express - and the
    difference is one flag and one oneof entry (tasks/048).

    A message field needs none of it: it has presence already. A
    repeated field can take none of it, and needs none - an absent
    repeated field IS an empty one (tasks/041).

    Synthetic oneofs must follow every real one in oneof_decl. Nothing
    built through here declares a real oneof; the one message that
    does, NixValue, is hand-built and carries no optional scalar."""
    oneof = msg.oneof_decl.add()
    oneof.name = f"_{f.name}"
    f.oneof_index = len(msg.oneof_decl) - 1
    f.proto3_optional = True
    return f


def _add_field(msg: Any, name: str, number: int, type_str: str,
               kinds: dict[str, str], optional: bool = False) -> Any:
    """Append one field of the declared surface type.

    Three shapes, and only the first is a plain lookup. A list is a
    repeated field of its element type. A map cannot be described by a
    type constant at all: proto3 spells it as a repeated field of a
    message the containing type carries, so this builds that message
    too.

    Optionality arrives two ways and means one thing. `T | None` is
    how a return type says it; `optional=True` is how a caller passes
    what a `_wire_fields` "?" or an omissible constructor parameter
    already decided. Either way the field is built for T and then
    given presence if it needs any."""
    if (inner := optional_value(type_str)) is not None:
        type_str, optional = inner, True
    if (value_type := map_value(type_str)) is not None:
        return _add_map_field(msg, name, number, value_type, kinds)
    if (item_type := list_value(type_str)) is not None:
        pt, message = _msg_arg_type(item_type, kinds)
        f = _field(msg, name, number, proto_type=pt, type_name=message)
        f.label = f.LABEL_REPEATED
        return f
    pt, message = _msg_arg_type(type_str, kinds)
    f = _field(msg, name, number, proto_type=pt, type_name=message)
    if optional and pt is not None:
        # pt is None exactly when the field is a message, and a
        # message field has presence already.
        _with_presence(msg, f)
    return f


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


def service_name(cls_name: str) -> str:
    return f"{cls_name}Service"


def _camel(method: str) -> str:
    """`add_to_store` -> `AddToStore`.

    A message name, not a method name. The rpcs keep the binding's own
    snake_case on purpose - they are the Python surface spelled once
    - while a message is a TYPE, and protobuf types are PascalCase.

    One helper because the two used to disagree: the request kept the
    snake_case and the response camel-cased it, so one method had two
    spellings in one schema."""
    return method.title().replace("_", "")


def req_name(cls_name: str, method: str) -> str:
    return f"{cls_name}_{_camel(method)}Req"


def resp_name(cls_name: str, method: str) -> str:
    # Class-prefixed: LocalStore and RemoteStore share method names, and
    # top-level message names must be unique across the file.
    return f"{cls_name}_{_camel(method)}Resp"


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


# Types that are DELIBERATELY not on the wire, and the reason each is.
#
# Distinct from everything else this function reports: the rest are
# gaps a later change could close, and these are decisions that a
# later change should not.
#
# Kept here rather than in `manifest.UNCROSSABLE`, which RAISES and
# stops the build. A method taking one of these is a real in-process
# method with no remote form - the same shape as `Store.real_path` -
# so it is REPORTED, and the protocol withholds it.
NOT_DATA = {
    "object": (
        "an arbitrary Python object is not data - here it is a "
        "callable the binding keeps and calls back. A remote client "
        "registering one would make the evaluator call BACK over the "
        "socket, on its own evaluation thread, once per invocation - "
        "a distributed call in a hot loop. In-process only, by "
        "decision rather than omission (tasks/033)."),
}


def wire_blocker(type_str: str, kinds: dict[str, str],
                 served: frozenset[str] | None = None) -> str | None:
    """Why this type cannot cross the wire, or None if it can.

    Reported rather than raised, so a function that is unrepresentable
    today still gets its in-process wrapper and the build says exactly
    what is missing.

    `served` names the classes that HAVE a service. None means "do not
    ask", which is what a caller testing a type in isolation wants;
    `annotate` passes the real set."""
    if (why := NOT_DATA.get(type_str)) is not None:
        return why
    # A proxy nobody serves. It crosses as a HANDLE, and a handle is
    # only worth having if some service takes one - so a method
    # returning this would publish an rpc whose answer no later call
    # can use.
    #
    # This was a SILENT skip until LogStream. Every unwrapped class
    # until then was also a wire VALUE, so "unwrapped" and "crosses by
    # copy" agreed, and `annotate` said so in a comment: "an unwrapped
    # class has no remote surface: it crosses as a value, so a caller
    # already holds the object". LogStream is unwrapped - pool, and no
    # method of it can block - and a proxy, which made that sentence
    # false and published `EvalState.subscribe_logs` answering a
    # handle with no service behind it (tasks/032).
    if served is not None and type_str not in served \
            and kinds.get(type_str) == "proxy":
        return (f"{type_str} is a proxy with no service: it crosses as a "
                f"handle, and nothing is wrapped to answer a call on that "
                f"handle. A remote caller would receive an id it cannot "
                f"use.")
    try:
        # `T | None` is T plus presence, so from here on it is T that
        # is under test - a field the schema cannot build has nothing
        # to be absent FROM.
        if (inner := optional_value(type_str)) is not None:
            if head(inner) in CONTAINERS:
                return (f"{type_str}: a repeated field has no presence and "
                        f"needs none - an absent container IS an empty one. "
                        f"Declare {inner} and return it empty.")
            if kinds.get(inner) == "proxy":
                return (f"{type_str}: a proxy return is adopted into a "
                        f"wrapper by every layer, and none of them adopts "
                        f"nothing. Absence would arrive as an object that is "
                        f"not one.")
            type_str = inner
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


def _method_blockers(m: Proto, kinds: dict[str, str],
                     served: frozenset[str] | None = None) -> list[str]:
    """Why this method has no RPC, or [] when it has one."""
    out = [
        f"parameter {p['name']!r}: {why}"
        for p in m["params"]
        if (why := wire_blocker(p["type"], kinds, served))
    ]
    if (why := wire_blocker(m["return_type"], kinds, served)):
        out.append(f"return type: {why}")
    return out


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
            # An unwrapped class gets no service. A wrapper buys two
            # things - a hop onto a home thread and a released GIL -
            # and a pool class whose methods cannot block needs
            # neither, so there is no async form for a handler to
            # await.
            #
            # This USED to read "it crosses as a value, so a caller
            # already holds the object", and that was true of every
            # unwrapped class until LogStream. It is not a rule: it
            # was a coincidence of which classes existed. What the
            # absence of a service actually means is now CHECKED,
            # below and in `wire_blocker`, rather than assumed here.
            if not proto["wrapped"]:
                continue
            proto["service"] = service_name(cls_name)
            if group == "wrappers":
                proto["acquire"] = {
                    "path": method_path(cls_name, ACQUIRE),
                    "req": req_name(cls_name, ACQUIRE),
                }

    kinds = _wire_kinds(manifest)
    # Which classes a handle can be USED with: the same two facts the
    # loop above stamps a service on, and not the stamp itself -
    # `cppgen/manifest` puts a `service` NAME on every proxy whether
    # or not one is published, so reading the key back would call
    # LogStream served and defeat the check.
    served = frozenset(
        name
        for group in ("wrappers", "returned_types")
        for name, proto in manifest[group].items()
        if proto["wrapped"] and proto["wire"] == "proxy")
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if not proto["wrapped"]:
                continue
            # A METHOD gets the same treatment a free function has
            # always had: say why it cannot cross, rather than raise
            # while building the schema. Not everything a binding
            # offers is a remote call - Store.real_path answers with a
            # path on the machine the store runs on - and such a
            # method still deserves its in-process wrapper.
            for m in proto["methods"]:
                m["wire_blockers"] = _method_blockers(m, kinds, served)
                if m["wire_blockers"]:
                    continue
                m["rpc"] = {
                    "path": method_path(cls_name, m["name"]),
                    "req": req_name(cls_name, m["name"]),
                    "resp": resp_name(cls_name, m["name"]),
                }

    for fname, proto in manifest.get("free_functions", {}).items():
        if not proto["wrapped"]:
            # No policy, so no wrapper and nothing to call remotely.
            # It is in the manifest to describe the module, not to be
            # published.
            proto["wire_blockers"] = [
                "no threading policy, so the function has no async form "
                "for a server to call"]
            continue
        blockers = _method_blockers(proto, kinds, served)
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
UNION = "union"
ERROR = "error"


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
    out.update({name: UNION for name in manifest.get("unions", {})})
    # An EXCEPTION class, which is a declared name and not a class in
    # the groups either. It already has a message - the fault detail
    # every typed error crosses in (tasks/036) - so a field of one
    # points at that rather than inventing a second shape.
    out.update({name: ERROR
                for name in (manifest.get("errors") or {}).get("classes", {})})
    return out


def _msg_arg_type(type_str: str,
                  kinds: dict[str, str]) -> tuple[int | None, str | None]:
    """Surface type string -> (proto_type_const|None, message_name|None)."""
    if type_str == "None":
        return None, None
    if (builtin := scalar_spelling(type_str)) is not None:
        # A declared type that goes in a field as a builtin. `str` is
        # itself; `datetime.timedelta` is an int of microseconds,
        # which is a fact about the WIRE and lives with the other
        # wire spellings rather than here.
        return _scalar_const(SCALARS[builtin]), None
    kind = kinds.get(type_str)
    if kind == UNION:
        # A SUM, as protobuf's own tagged union. One message per
        # alias, holding one `oneof` - which is why the alias needed a
        # NAME: the message is called after it.
        return None, union_msg_name(type_str)
    if kind == ENUM:
        # A StrEnum member IS a str. Nothing about the transport
        # changes; the type exists for the caller, not for the wire.
        return _scalar_const(SCALARS["str"]), None
    if kind == ERROR:
        # The same message the status details carry. One shape for one
        # error class, whether it arrives as the failure of a call or
        # as a field of a value that is reporting one.
        return None, fault_msg_name(type_str)
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
            _add_field(m, fname, n, ftype.removesuffix("?"), kinds,
                       optional=ftype.endswith("?"))


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
                # "?" reaches the schema now. It used to be a codec
                # concern only, and the codec answered it by reading
                # an empty string as absent (tasks/048).
                _add_field(m, fname, n, ftype.removesuffix("?"), kinds,
                           optional=ftype.endswith("?"))

    # ...and one message per UNION, holding one oneof.
    #
    # This is where gRPC beats both of Nix's own encodings. The daemon
    # sends a DerivedPath as a string and parses it back; the JSON
    # form tags the arms by SHAPE - a string means opaque, `["*"]`
    # means all outputs - and both work only because the alternatives
    # happen not to collide. A oneof is a real tag, and protobuf lets
    # a message hold itself, so the recursive arm needs nothing said
    # about it here (tasks/059).
    for alias, arms in manifest.get("unions", {}).items():
        m = file_dp.message_type.add()
        m.name = union_msg_name(alias)
        one = m.oneof_decl.add()
        one.name = "raw"
        for n, arm in enumerate(arms, start=1):
            # An arm is never `optional`: the oneof IS the presence,
            # and marking a member optional would add a second,
            # disagreeing one.
            f = _add_field(m, arm_field(arm), n, arm, kinds)
            f.oneof_index = 0


def _add_service(file_dp: Any, cls_name: str, proto: Proto,
                 kinds: dict[str, str]) -> None:
    svc = file_dp.service.add()
    svc.name = proto["service"]

    if "acquire" in proto:
        req = file_dp.message_type.add()
        req.name = proto["acquire"]["req"]
        for n, param in enumerate(proto["ctor"], start=1):
            # The client skips a None argument and the server decodes
            # with optional=True, so the field has to be able to say
            # "absent" rather than lean on an empty string.
            _add_field(req, param["name"], n, param["type"], kinds,
                       optional=param["default"] == "None")
        rpc = svc.method.add()
        rpc.name = ACQUIRE
        rpc.input_type = f".{PKG}.{req.name}"
        rpc.output_type = f".{PKG}.{HANDLE}"

    for m in proto["methods"]:
        if "rpc" not in m:
            continue  # no wire representation; annotate() said why
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


def _add_session(f: Any, kinds: dict[str, str]) -> None:
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

    # No token field: it rides in the x-huggorm-conn metadata like
    # every other rpc's does. An empty request message is the right
    # shape for a probe whose only question is "am I still bound"
    # (tasks/049).
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
    _add_log_stream(f, sess, kinds)


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


def _options(req: Any, kinds: dict[str, str], first: int) -> None:
    """The two fields a log subscription carries, wherever it is made.

    Stated once because both requests carry them, and the numbering
    is a parameter because only one of the two has a handle in front.

    `level` gets real presence and `capacity` does not, and the two
    are not the same question. Level 0 is lvlError, which is a
    subscription somebody means - "errors only" - so an unset field
    and a zero one have to differ. Capacity 0 is not a queue at all,
    so zero is free to mean "the binding's own default" (tasks/048).
    """
    _add_field(req, "capacity", first, "int", kinds)
    _add_field(req, "level", first + 1, "int", kinds, optional=True)


def _add_log_stream(f: Any, sess: Any, kinds: dict[str, str]) -> None:
    """The one rpc that travels the other way, unsolicited.

    Hand-written, like the rest of Session, and for the reason
    `tasks/032` gives: a log stream is PROTOCOL. Every other rpc came
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
    _options(req, kinds, 2)

    # The same subscription with nothing to name. The process-wide
    # sink takes records no subscribed thread claimed, so there is no
    # handle to address and the message is the options alone
    # (`tasks/085`).
    #
    # Numbered from 1 rather than leaving a hole where the handle
    # would be. They are two messages, not one message with a field
    # switched off, and a reserved gap would say the opposite.
    process = f.message_type.add()
    process.name = "ProcessLogsReq"
    _options(process, kinds, 1)

    # ONE response message for both. A batch of records and a drop
    # count is the whole answer either way, and a second message with
    # the same two fields would be the same fact declared twice.
    resp = f.message_type.add()
    resp.name = "LogsResp"
    _add_field(resp, "records", 1, "list[LogRecord]", kinds)
    _add_field(resp, "dropped", 2, "int", kinds)

    for name, input_name in (("Logs", "LogsReq"),
                             ("ProcessLogs", "ProcessLogsReq")):
        rpc = sess.method.add()
        rpc.name = name
        rpc.input_type = f".{PKG}.{input_name}"
        rpc.output_type = f".{PKG}.LogsResp"
        rpc.server_streaming = True


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
    kinds = _wire_kinds(manifest)
    _add_session(f, kinds)

    # Returned types expose methods through handles as well: their
    # operations run wherever the producing wrapper put them.
    for group in ("wrappers", "returned_types"):
        for cls_name, proto in manifest[group].items():
            if proto["wrapped"]:
                _add_service(f, cls_name, proto, kinds)
    _add_free_service(f, manifest, kinds)
    return bytes(fds.SerializeToString())
