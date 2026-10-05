"""How one emitted module writes the types it names, and what that
needs imported (huggorm#29).

A surface renames only the PROXY leaves - to a protocol, an async
class or an RPC class - and keeps every structure around them. Every
other leaf is written as itself, and its kind says where it comes
from. So an import is decided by what a type IS, not by reading the
emitted text back.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable

from huggorm_gen.ir import ParamModel, TypeRef

# Names Python already has.
BUILTIN = frozenset({"None", "str", "int", "float", "bool", "bytes",
                     "object"})


class Spelling:
    """One module's renderer. `proxy` names a proxy leaf on this
    surface, and returns the name and where it comes from: "defined"
    (this module), "protocols" (the protocols module) or "bindings"."""

    def __init__(self, proxy: Callable[[TypeRef], tuple[str, str]]) -> None:
        self._proxy = proxy
        self.bindings: set[str] = set()
        self.unions: set[str] = set()
        self.protocols: set[str] = set()
        self.modules: set[str] = set()

    def __call__(self, t: TypeRef) -> str:
        if t.optional:
            return f"{self(t.args[0])} | None"
        if t.origin == "list":
            return f"list[{self(t.args[0])}]"
        if t.origin == "dict":
            return f"dict[str, {self(t.args[0])}]"
        return self._leaf(t)

    def _leaf(self, t: TypeRef) -> str:
        if t.kind == "proxy":
            name, source = self._proxy(t)
            self._need(name, source)
            return name
        if t.kind == "module":
            self.modules.add(t.name.split(".", 1)[0])
        elif t.kind == "union":
            self.unions.add(t.name)
        elif t.name not in BUILTIN:
            self.bindings.add(t.name)
        return t.name

    def _need(self, name: str, source: str) -> None:
        if source == "protocols":
            self.protocols.add(name)
        elif source == "bindings":
            self.bindings.add(name)

    def returns(self, t: TypeRef | None) -> str:
        return self(t) if t is not None else "None"

    def defaults(self, params: Iterable[ParamModel]) -> None:
        """The vocabulary a member default names is an import too."""
        for p in params:
            if p.default_class:
                self.bindings.add(p.default_class)

    def imports(self) -> list[ast.stmt]:
        """`import pathlib`, then the bindings, the unions and the
        protocols, each sorted - the order every module writes them."""
        out: list[ast.stmt] = [ast.Import(names=[ast.alias(name=m)])
                               for m in sorted(self.modules)]
        if self.bindings:
            out.append(ast.ImportFrom(
                module="huggorm_bindings",
                names=[ast.alias(name=n) for n in sorted(self.bindings)],
                level=0))
        for module, names in (("_unions", self.unions),
                              ("protocols", self.protocols)):
            if names:
                out.append(ast.ImportFrom(
                    module=module,
                    names=[ast.alias(name=n) for n in sorted(names)],
                    level=1))
        return out
