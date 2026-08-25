"""
Contracts that hold without a server running.
"""

import ast
import pathlib
from typing import Any


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
    here = pathlib.Path(__file__).resolve().parent.parent / "cythonix"
    offenders = []
    for mod in ("server.py", "remote.py", "wire.py", "lifecycle.py", "grpc_pb.py"):
        tree = ast.parse((here / mod).read_text(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in domain:
                offenders.append(f"{mod}:{node.lineno}: {node.value!r}")
    assert not offenders, "; ".join(offenders)
