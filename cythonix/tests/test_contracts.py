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
