"""
A failure, across the wire, as messages rather than as text.

A failed unary call carries no response message - only a status - so
the status is the only channel a failure has. gRPC's own answer for
that is `grpc-status-details-bin`: a `google.rpc.Status` whose
`details` is a repeated `Any`. This module is that, against the schema
this repo already builds.

It replaces a JSON blob stuffed into the status MESSAGE, which is the
human-readable field. That worked, and it was the wrong shape twice
over: a text field carrying structure, and a class rebuilt from a name
looked up in a hard-coded map of five builtins.

Two details travel:

- `Fault` - the code, the message, and the cause approximated by name.
  Unchanged in meaning, and it is what a peer gets when it cannot
  resolve anything else.
- the cause AS ITSELF, when the bindings declare its class. Then the
  `Any`'s type name is the identity, and the far side resolves it in
  the schema pool or not at all. No class name has to be trusted,
  because no class name crosses on its own.

grpclib takes any StatusDetailsCodecBase, which is what makes this
possible: its own ProtoStatusDetailsCodec resolves detail types
through protobuf's DEFAULT symbol database, and these descriptors live
in a private pool built from grpc_schema.pb at import.
"""

import importlib
from collections.abc import Sequence
from types import ModuleType
from typing import Any

from google.protobuf import message_factory
from grpclib.const import Status
from grpclib.encoding.base import StatusDetailsCodecBase

from . import grpc_pb as schema

# The suffix grpc_schema gives an error class's message. One place,
# because the schema builder writes it and this module reads it back.
FAULT_SUFFIX = "Fault"
FAULT = "Fault"


class SchemaStatusDetails(StatusDetailsCodecBase):
    """google.rpc.Status details, resolved against the schema pool.

    Same wire bytes as grpclib's own codec - a google.rpc.Status with
    Any-packed details - and a different place to look the types up.
    An unresolvable detail is DROPPED rather than reported as an
    opaque placeholder: a caller that cannot read a detail is in the
    same position as one that never got it, and the Fault detail
    beside it still says what failed."""

    def __init__(self, pool: Any) -> None:
        self.pool = pool

    def _class_for(self, full_name: str) -> Any | None:
        try:
            desc = self.pool.FindMessageTypeByName(full_name)
        except KeyError:
            return None
        return message_factory.GetMessageClass(desc)  # type: ignore[no-untyped-call]

    def encode(self, status: Status, message: str | None,
               details: Any) -> bytes:
        from google.rpc import status_pb2

        proto = status_pb2.Status(code=status.value, message=message)
        for detail in details or ():
            proto.details.add().Pack(detail)
        out: bytes = proto.SerializeToString()  # type: ignore[no-untyped-call]
        return out

    def decode(self, status: Status, message: str | None,
               data: bytes) -> Sequence[Any]:
        from google.rpc import status_pb2

        proto = status_pb2.Status.FromString(data)
        out = []
        for container in proto.details:
            kls = self._class_for(container.TypeName())
            if kls is None:
                continue
            detail = kls()
            container.Unpack(detail)
            out.append(detail)
        return out


class FaultCodec:
    """One failure, as the details it travels as and back again.

    Reads the manifest for which exception classes are declared, and
    the schema pool for the messages they travel as. Names no error
    type itself, the same way WireCodec names no value type."""

    def __init__(self, manifest: dict[str, Any], pool: Any,
                 module: ModuleType | None = None) -> None:
        table: dict[str, Any] = manifest.get("errors") or {}
        self.module_name: str | None = table.get("module")
        self.fields: dict[str, list[list[str]]] = {
            name: proto["wire_fields"]
            for name, proto in table.get("classes", {}).items()
        }
        self.pool = pool
        self._module = module

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            if self.module_name is None:
                raise TypeError("this manifest declares no error module")
            self._module = importlib.import_module(self.module_name)
        return self._module

    def _msg(self, name: str) -> Any:
        return message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
            self.pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
                f"{schema.PKG}.{name}"))

    # -- server side ------------------------------------------------------
    def details(self, wrapper: Any) -> list[Any]:
        """The detail messages for one wrapper error.

        Always a Fault. Also the cause as its own message, when the
        bindings declare its class - and only when the cause IS that
        class, not merely something sharing its name."""
        cause = getattr(wrapper, "__cause__", None)
        fault = self._msg(FAULT)()
        fault.code = wrapper.code
        fault.message = wrapper.message
        if cause is not None:
            fault.cause_type = type(cause).__name__
            fault.cause_message = str(cause)
        out = [fault]
        declared = self._declared(cause)
        if declared is not None:
            msg = self._msg(declared + FAULT_SUFFIX)()
            for fname, _ in self.fields[declared]:
                setattr(msg, fname, str(getattr(cause, fname)))
            out.append(msg)
        return out

    def _declared(self, cause: BaseException | None) -> str | None:
        """The declared class name of `cause`, or None.

        Identity, not the name: an exception that happens to share a
        name with a declared one is a different class, and sending it
        as that class would be a lie about what failed."""
        if cause is None or self.module_name is None:
            return None
        name = type(cause).__name__
        if name not in self.fields:
            return None
        if type(cause) is not getattr(self.module, name, None):
            return None
        return name

    # -- client side ------------------------------------------------------
    def rebuild(self, details: Sequence[Any] | None) -> BaseException | None:
        """The exception a set of details describes, or None.

        None means the details said nothing this can use - no Fault, or
        one from a peer built against a different schema. The caller
        then keeps the transport error it already has, because failing
        to rebuild an error must never replace it with a different
        one."""
        from cythonix_generated._runtime import InternalError, WrapperError

        found = list(details or ())
        fault = self._named(found, FAULT)
        if fault is None:
            return None
        if not fault.cause_type:
            return WrapperError(fault.message)
        return InternalError(fault.message, cause=self._cause(found, fault))

    def _cause(self, details: Sequence[Any], fault: Any) -> BaseException:
        """The cause, rebuilt from its own message when one came, and
        approximated from the Fault when none did."""
        for name, fields in self.fields.items():
            msg = self._named(details, name + FAULT_SUFFIX)
            if msg is None:
                continue
            kls = getattr(self.module, name, None)
            if isinstance(kls, type) and issubclass(kls, BaseException):
                return kls(*(getattr(msg, fname) for fname, _ in fields))
        return _approximate(fault.cause_type, fault.cause_message)

    @staticmethod
    def _named(details: Sequence[Any], name: str) -> Any | None:
        want = f"{schema.PKG}.{name}"
        for d in details:
            if d.DESCRIPTOR.full_name == want:
                return d
        return None


# What a cause becomes when nothing typed it. The builtins a binding
# raises through the runtime, and Exception for anything else - the
# same approximation this layer has always made, now confined to the
# case it is actually for.
_BUILTINS: dict[str, type[BaseException]] = {
    "ValueError": ValueError, "TypeError": TypeError,
    "RuntimeError": RuntimeError, "KeyError": KeyError, "OSError": OSError,
}


def _approximate(type_name: str, message: str) -> BaseException:
    return _BUILTINS.get(type_name, Exception)(message)
