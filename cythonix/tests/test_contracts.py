"""
Contracts that hold without a server running.
"""

import ast
import pathlib
from typing import Any

import pytest


def test_no_hardcoded_domain_types(manifest: dict[str, Any]) -> None:
    """No layer above the bindings may name a domain type.

    Wire policy is declared next to the binding and reaches the schema,
    the server and the client through the manifest. A type name written
    into any of them is the duplication this design exists to remove:
    it means adding a class needs edits in four places, and forgetting
    one fails at the first call that touches it, not at build time."""
    domain = {
        name
        for group in ("wrappers", "returned_types")
        for name in manifest[group]
    }
    # Exception classes are domain types too, and the same rule holds
    # for the same reason: which errors exist is the bindings' to
    # declare, so no layer above them may carry a list of them
    # (tasks/036). The builtins this layer raises ITSELF - KeyError for
    # an unknown handle - are not in that set and are not the subject.
    domain |= set((manifest.get("errors") or {}).get("classes", {}))
    here = pathlib.Path(__file__).resolve().parent.parent / "cythonix"
    offenders = []
    for mod in ("server.py", "remote.py", "wire.py", "faults.py",
                "lifecycle.py", "grpc_pb.py"):
        tree = ast.parse((here / mod).read_text(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in domain:
                offenders.append(f"{mod}:{node.lineno}: {node.value!r}")
    assert not offenders, "; ".join(offenders)


def test_a_declared_error_crosses_as_its_own_message(
        manifest: dict[str, Any]) -> None:
    """The class identity is the MESSAGE TYPE, not a name to look up.

    That is the whole reason a fault travels in the status details
    rather than as text: an Any carries the type name of a message,
    the far side resolves it in the schema pool, and a name it cannot
    resolve resolves to nothing. Nothing has to decide whether a class
    name is safe to construct, because no class name crosses on its
    own (tasks/036).

    Needs no server: this is what the server would put on the wire."""
    from cythonix.faults import FaultCodec
    from cythonix.grpc_pb import PKG, load_pool
    from cythonix_bindings.errors import BadStorePath
    from cythonix_generated._runtime import InternalError

    codec = FaultCodec(manifest, load_pool())
    failed = InternalError("Store.parse_store_path failed",
                           cause=BadStorePath("plain", "coloured"))
    names = [d.DESCRIPTOR.full_name for d in codec.details(failed)]
    assert names == [f"{PKG}.Fault", f"{PKG}.BadStorePathFault"], names

    # An UNDECLARED cause carries no message of its own, so the far
    # side gets the Fault and nothing to resolve.
    plain = InternalError("something else failed", cause=ValueError("nope"))
    assert [d.DESCRIPTOR.full_name for d in codec.details(plain)] \
        == [f"{PKG}.Fault"]


def test_every_declared_error_has_a_message(manifest: dict[str, Any]) -> None:
    """One message per declared class, emitted from the manifest.

    The schema builder loops the error table; it names no class. So
    adding an error to the bindings adds its message here, and this
    holds the two in step."""
    from cythonix.grpc_pb import PKG, load_pool

    pool = load_pool()
    declared = (manifest.get("errors") or {}).get("classes", {})
    assert declared, "the bindings declare no errors at all"
    for name, proto in declared.items():
        desc = pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
            f"{PKG}.{name}Fault")
        assert [f.name for f in desc.fields] == [
            fname for fname, _ in proto["wire_fields"]], name


def test_a_string_enum_decodes_to_its_class(manifest: dict[str, Any]) -> None:
    """A value read off the wire comes back typed.

    A StrEnum crosses as a plain string - it IS one - so nothing about
    the transport changes. What the manifest's enum table buys is the
    other direction: the codec knows which class to rebuild, so a
    caller gets ContentAddressMethod.FLAT rather than "flat", and a
    value that is not a member raises here instead of reaching
    libstore.

    Needs no server: this is the converter both sides use."""
    from cythonix.wire import WireCodec
    from cythonix_bindings import ContentAddressMethod

    codec = WireCodec(manifest)
    assert manifest["enums"], "the bindings declare no vocabularies"
    assert codec.kind("ContentAddressMethod") == "scalar"

    rebuild = codec.scalar("ContentAddressMethod")
    assert rebuild("flat") is ContentAddressMethod.FLAT
    with pytest.raises(ValueError, match="not a valid"):
        rebuild("nonsense")

    # ...and a built-in scalar still resolves to the builtin.
    assert codec.scalar("bytes") is bytes


def _probe_message() -> Any:
    """A message with one repeated string and one map<string, string>.

    Built here rather than borrowed from the real schema, because no
    binding declares an enum container yet - which is the point of
    tasks/047. The two field shapes are the ones the schema builder
    gives an enum, since an enum crosses as a string.

    Hand-built the way grpc_schema builds one: a map is not a type
    constant in proto3, it is a repeated field of an entry message the
    containing type carries."""
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    file_dp = descriptor_pb2.FileDescriptorProto()  # type: ignore[attr-defined]
    file_dp.name, file_dp.package, file_dp.syntax = "probe.proto", "probe", "proto3"
    msg = file_dp.message_type.add()
    msg.name = "Probe"

    words = msg.field.add()
    words.name, words.number = "words", 1
    words.type, words.label = words.TYPE_STRING, words.LABEL_REPEATED

    entry = msg.nested_type.add()
    entry.name = "TableEntry"
    entry.options.map_entry = True
    for name, number in (("key", 1), ("value", 2)):
        f = entry.field.add()
        f.name, f.number = name, number
        f.type, f.label = f.TYPE_STRING, f.LABEL_OPTIONAL
    table = msg.field.add()
    table.name, table.number = "table", 2
    table.type, table.label = table.TYPE_MESSAGE, table.LABEL_REPEATED
    table.type_name = ".probe.Probe.TableEntry"

    pool = descriptor_pool.DescriptorPool()
    pool.Add(file_dp)  # type: ignore[no-untyped-call]
    # protobuf ships no stubs for its own factory or pool lookups.
    return message_factory.GetMessageClass(  # type: ignore[no-untyped-call]
        pool.FindMessageTypeByName(  # type: ignore[no-untyped-call]
            "probe.Probe"))()


def test_an_enum_survives_a_container(manifest: dict[str, Any]) -> None:
    """An enum is a scalar, and a container does not change that.

    The schema already said so - wire_blocker accepts an enum
    anywhere a scalar goes - while the codec's container helpers
    indexed the raw scalar TABLE, which holds no enum. Two of the four
    sites raised KeyError on the first call; the other two returned
    bare strs and broke the promise the test above asserts for a
    singular field.

    Nothing declares an enum container today, so the case is built
    here. That is the whole reason it stayed latent."""
    from cythonix.wire import WireCodec
    from cythonix_bindings import ContentAddressMethod as CA
    from cythonix_bindings import HashAlgorithm

    codec = WireCodec(manifest)
    probe = _probe_message()

    codec.list_to_msg("list[HashAlgorithm]",
                      [HashAlgorithm.SHA256, HashAlgorithm.SHA512],
                      probe.words)
    assert list(probe.words) == ["sha256", "sha512"], "a member IS its string"
    assert codec.list_from_msg("list[HashAlgorithm]", probe.words) == [
        HashAlgorithm.SHA256, HashAlgorithm.SHA512]
    assert all(isinstance(v, HashAlgorithm)
               for v in codec.list_from_msg("list[HashAlgorithm]", probe.words))

    codec.map_to_msg("dict[str, ContentAddressMethod]",
                     {"a": CA.NAR, "b": CA.FLAT}, probe.table)
    assert dict(probe.table) == {"a": "nar", "b": "flat"}
    assert codec.map_from_msg("dict[str, ContentAddressMethod]", probe.table) == {
        "a": CA.NAR, "b": CA.FLAT}
    assert all(isinstance(v, CA) for v in
               codec.map_from_msg("dict[str, ContentAddressMethod]",
                                  probe.table).values())

    # A member that is not one raises here rather than reaching
    # libstore - the same guarantee a singular field has.
    probe.words.append("nonsense")
    with pytest.raises(ValueError, match="not a valid"):
        codec.list_from_msg("list[HashAlgorithm]", probe.words)


def test_a_method_with_no_wire_form_is_absent_everywhere(
        manifest: dict[str, Any]) -> None:
    """A method the wire cannot carry leaves three places at once.

    Not everything a binding offers is a remote call. Store.real_path
    answers with a filesystem path on the machine the store runs on,
    and pathlib.Path is not a wire type - so the schema has no message
    for it, the server publishes no handler, and the protocol cannot
    promise it because a protocol is what BOTH implementations satisfy.

    What it does NOT lose is the in-process wrapper. That is the whole
    distinction: local and remote are different surfaces, and this is
    the machinery that lets them differ without either one lying."""
    from cythonix_generated import AsyncStore
    from cythonix_generated.rpc import RPCStore

    store = manifest["wrappers"]["Store"]
    blocked = {m["name"]: m for m in store["methods"] if m["wire_blockers"]}
    assert "real_path" in blocked, sorted(blocked)

    for name, m in blocked.items():
        assert "rpc" not in m, f"{name} is blocked and still has an rpc"
        # A wire blocker is a protocol blocker: the remote surface
        # cannot offer it, so the shared one cannot declare it.
        assert m["protocol_blockers"], name
        assert hasattr(AsyncStore, name), f"{name} lost its wrapper too"
        assert not hasattr(RPCStore, name), f"{name} is on the rpc client"
        assert name not in RPCStore._rpc, f"{name} has a call spec"


def test_an_untyped_cause_rebuilds_from_builtins_only() -> None:
    """The approximation names no types of its own.

    It used to hold a table of five builtins, which was wrong twice:
    every OTHER builtin silently became a bare Exception, and adding
    one meant editing a file above the bindings.

    The rule is now one sentence - resolve in `builtins`, and only if
    it is an exception class - so what may be constructed is bounded
    without anything keeping a list. Nothing else the peer names gets
    near a constructor, and a name that vanished before is now kept in
    the message."""
    from cythonix.faults import _approximate

    # A builtin exception rebuilds as itself.
    for name in ("ValueError", "KeyError", "OSError", "IndexError",
                 "ZeroDivisionError", "StopAsyncIteration"):
        out = _approximate(name, "boom")
        assert type(out).__name__ == name, out

    # A builtin that is NOT an exception is never constructed...
    for name in ("print", "dict", "object", "type"):
        assert type(_approximate(name, "boom")) is Exception, name
    # ...nor is anything that is not a builtin at all.
    for name in ("Store", "NixError", "os.system", ""):
        assert type(_approximate(name, "boom")) is Exception, name

    # A builtin whose constructor wants more than a message degrades
    # rather than raising, and the name survives in the text.
    out = _approximate("UnicodeDecodeError", "boom")
    assert type(out) is Exception and "UnicodeDecodeError" in str(out)
