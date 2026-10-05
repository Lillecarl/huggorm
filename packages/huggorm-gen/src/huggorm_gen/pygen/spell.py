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
from collections.abc import Callable, Iterable, Mapping

from huggorm_gen.ir import ParamModel, TypeRef

# Names Python already has.
BUILTIN = frozenset({"None", "str", "int", "float", "bool", "bytes",
                     "object"})

# Where a renamed proxy comes from: this module ("defined"), the
# protocols module, a sibling async module, or the bindings.
Rename = Callable[[TypeRef], tuple[str, str]]


def _async_module(name: str) -> str:
    """The module an async class lives in: `AsyncStore` is in
    `async_store`."""
    return f"async_{name.removeprefix('Async').lower()}"


class Spelling:
    """One module's renderer, and the imports what it wrote needs.

    `proxy` renames a proxy leaf unless a call passes its own: one
    module may spell a parameter, a constructor argument and a return
    three ways."""

    def __init__(self, proxy: Rename | None = None,
                 expand: Mapping[str, tuple[str, ...]] | None = None) -> None:
        self._proxy = proxy
        # Union arms to write a union out as, instead of naming its
        # alias: a binding stub, because a compiled module holds no
        # alias for a typechecker to find.
        self._expand = expand
        self.bindings: set[str] = set()
        self.unions: set[str] = set()
        self.protocols: set[str] = set()
        self.modules: set[str] = set()
        self.siblings: set[str] = set()

    def __call__(self, t: TypeRef, proxy: Rename | None = None) -> str:
        if t.optional:
            return f"{self(t.args[0], proxy)} | None"
        if t.origin == "list":
            return f"list[{self(t.args[0], proxy)}]"
        if t.origin == "dict":
            return f"dict[str, {self(t.args[0], proxy)}]"
        return self._leaf(t, proxy or self._proxy)

    def _leaf(self, t: TypeRef, proxy: Rename | None) -> str:
        if t.kind == "proxy":
            if proxy is None:
                raise TypeError(f"no spelling for the proxy {t.name}")
            name, source = proxy(t)
            self.need(name, source)
            return name
        if t.kind == "module":
            self.module(t.name)
        elif t.kind == "union":
            if self._expand is not None:
                return self._arms(t.name)
            self.unions.add(t.name)
        elif t.name not in BUILTIN:
            self.bindings.add(t.name)
        return t.name

    def _arms(self, union: str) -> str:
        assert self._expand is not None
        out = []
        for arm in self._expand[union]:
            if arm in self._expand:
                out.append(self._arms(arm))
                continue
            if arm not in BUILTIN:
                self.bindings.add(arm)
            out.append(arm)
        return " | ".join(out)

    def need(self, name: str, source: str) -> None:
        if source == "protocols":
            self.protocols.add(name)
        elif source == "bindings":
            self.bindings.add(name)
        elif source == "siblings":
            self.siblings.add(name)

    def module(self, dotted: str) -> None:
        """A type written dotted, `pathlib.Path`: its module is bound."""
        self.modules.add(dotted.split(".", 1)[0])

    def returns(self, t: TypeRef | None, proxy: Rename | None = None) -> str:
        return self(t, proxy) if t is not None else "None"

    def defaults(self, params: Iterable[ParamModel]) -> None:
        """The vocabulary a member default names is an import too."""
        for p in params:
            if p.default_class:
                self.bindings.add(p.default_class)

    def absorb(self, other: Spelling) -> None:
        """Take another renderer's imports, for a module that spells
        parameters and returns differently."""
        self.bindings |= other.bindings
        self.unions |= other.unions
        self.protocols |= other.protocols
        self.modules |= other.modules
        self.siblings |= other.siblings

    def sibling_imports(self, own: str = "") -> list[ast.stmt]:
        """`from .async_x import AsyncX`, one per sibling, never this
        module's own class."""
        return [ast.ImportFrom(module=_async_module(n),
                               names=[ast.alias(name=n)], level=1)
                for n in sorted(self.siblings - {own})]

    def module_imports(self) -> list[ast.stmt]:
        return [ast.Import(names=[ast.alias(name=m)])
                for m in sorted(self.modules)]

    def binding_imports(self, exclude: Iterable[str] = ()) -> list[ast.stmt]:
        """The bindings, then the unions, then the protocols, each
        sorted."""
        out: list[ast.stmt] = []
        bound = sorted(self.bindings - set(exclude))
        if bound:
            out.append(ast.ImportFrom(
                module="huggorm_bindings",
                names=[ast.alias(name=n) for n in bound], level=0))
        for module, names in (("_unions", self.unions),
                              ("protocols", self.protocols)):
            if names:
                out.append(ast.ImportFrom(
                    module=module,
                    names=[ast.alias(name=n) for n in sorted(names)],
                    level=1))
        return out

    def imports(self) -> list[ast.stmt]:
        """`import pathlib`, then the binding groups: the order the
        all-in-one modules write them in."""
        return self.module_imports() + self.binding_imports()
