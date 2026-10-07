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

from huggorm_gen.ir import Kind, Origin, ParamModel, TypeRef
from huggorm_gen.payload.wiretypes import python_spelling

# Names Python already has.
BUILTIN = frozenset({"None", "str", "int", "float", "bool", "bytes",
                     "object"})

# Where a name is imported from: a module and its relative level.
Source = tuple[str, int]
BINDINGS: Source = ("huggorm_bindings", 0)
UNIONS: Source = ("_unions", 1)
PROTOCOLS: Source = ("protocols", 1)


def sibling(cls: str) -> Source:
    """The module a class's async form lives in: `Store`'s is
    `async_store`."""
    return (f"async_{cls.lower()}", 1)


# A renamed proxy, and where it comes from: None when the module
# defines it.
Rename = Callable[[TypeRef], tuple[str, Source | None]]


def import_from(module: str, *names: str, level: int = 0) -> ast.ImportFrom:
    """`from module import names`. ruff merges and orders them later."""
    return ast.ImportFrom(module=module, names=[ast.alias(name=n) for n in names],
                          level=level)


class Spelling:
    """One module's renderer, and the imports what it wrote needs.

    `proxy` renames a proxy leaf unless a call passes its own: one
    module may spell a parameter, a constructor argument and a return
    three ways."""

    def __init__(self, proxy: Rename | None = None,
                 expand: Mapping[str, tuple[TypeRef, ...]] | None = None) -> None:
        self._proxy = proxy
        # Union arms to write a union out as, instead of naming its
        # alias: a binding stub, because a compiled module holds no
        # alias for a typechecker to find.
        self._expand = expand
        # `from module import name`, by where it comes from.
        self.froms: dict[Source, set[str]] = {}
        # `import module`, for a type written dotted.
        self.modules: set[str] = set()

    def __call__(self, t: TypeRef, proxy: Rename | None = None,
                 twin: bool = False) -> str:
        if t.optional:
            return f"{self(t.args[0], proxy, twin)} | None"
        if t.origin is Origin.LIST:
            return f"list[{self(t.args[0], proxy, twin)}]"
        if t.origin is Origin.DICT:
            return f"dict[str, {self(t.args[0], proxy, twin)}]"
        if twin and t.twin:
            self.module(t.twin)
            return t.twin
        return self._leaf(t, proxy or self._proxy)

    def _leaf(self, t: TypeRef, proxy: Rename | None) -> str:
        if t.kind == Kind.PROXY:
            if proxy is None:
                raise TypeError(f"no spelling for the proxy {t.name}")
            name, source = proxy(t)
            self.need(name, source)
            return name
        if t.kind == Kind.MODULE:
            self.module(t.name)
        elif t.kind == Kind.UNION:
            if self._expand is not None:
                return self._arms(t.name)
            self.need(t.name, UNIONS)
        elif t.name not in BUILTIN:
            self.need(t.name, BINDINGS)
        return t.name

    def _arms(self, union: str) -> str:
        assert self._expand is not None
        out = []
        for arm in self._expand[union]:
            if arm.kind == Kind.UNION:
                out.append(self._arms(arm.name))
                continue
            name = python_spelling(arm.name)
            if name not in BUILTIN:
                self.need(name, BINDINGS)
            out.append(name)
        return " | ".join(out)

    def need(self, name: str, source: Source | None) -> None:
        if source is not None:
            self.froms.setdefault(source, set()).add(name)

    @property
    def bindings(self) -> set[str]:
        return self.froms.get(BINDINGS, set())

    def module(self, dotted: str) -> None:
        """A type written dotted, `pathlib.Path`: its module is bound."""
        self.modules.add(dotted.split(".", 1)[0])

    def returns(self, t: TypeRef | None, proxy: Rename | None = None,
                twin: bool = False) -> str:
        return self(t, proxy, twin) if t is not None else "None"

    def defaults(self, params: Iterable[ParamModel]) -> None:
        """The vocabulary a member default names is an import too."""
        for p in params:
            if p.default_class:
                self.need(p.default_class, BINDINGS)

    def absorb(self, other: Spelling) -> None:
        """Take another renderer's imports, for a module that spells
        parameters and returns differently."""
        for source, names in other.froms.items():
            self.froms.setdefault(source, set()).update(names)
        self.modules |= other.modules

    def module_imports(self) -> list[ast.stmt]:
        return [ast.Import(names=[ast.alias(name=m)]) for m in self.modules]

    def imports(self, own: str = "") -> list[ast.stmt]:
        """Every import what this renderer wrote needs. `own` is the
        class the module defines, which it must not import."""
        return self.module_imports() + [
            import_from(module, *(names - {own}), level=level)
            for (module, level), names in self.froms.items() if names - {own}]
