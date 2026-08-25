"""
Consume the bindings pxd (e.g. c_store.pxd) using Cython's own parser.

No regex, no compiled-artifact introspection: the pxd IS the machine-
readable declaration surface, and this module turns it into plain dicts
with FULL parameter and return types.

API is pinned-Cython-specific (3.2.5 from nixpkgs - verified against the
extracted source, NOT against any checkout):
    Parsing.p_module(scanner, in_pxd=True, ...)
    ModuleNode.body.stats -> [FromCImportStatNode | CDefExternNode]
    CDefExternNode.body   -> [CppClassNode] (+ free CFuncDefNode)
    CppClassNode.attributes -> [CVarDefNode] whose declarators contain a
        CFuncDeclaratorNode carrying name/args/exception_check/
        is_const_method; arg and declarator chains may be wrapped in
        CReference/CConst nodes.
"""

from io import StringIO
from typing import Any

from Cython.Compiler import Parsing
from Cython.Compiler.Scanning import PyrexScanner

# StringSourceDescriptor is public in practice and absent from
# Cython's __all__, so a typechecker cannot see it.
from Cython.Compiler.TreeFragment import (
    StringParseContext,
    StringSourceDescriptor,  # type: ignore[attr-defined]
)

Proto = dict[str, Any]

# No type table here on purpose: this module reports the raw C names and
# model.py owns the mapping, so the two cannot drift apart.


def parse_pxd_module(name: str, text: str) -> Any:
    context = StringParseContext(name)
    # Cython ships no annotations, so every call into it is untyped.
    scope = context.find_module(name, need_pxd=False)  # type: ignore[no-untyped-call]
    src = StringSourceDescriptor(name, text)
    scanner = PyrexScanner(StringIO(text), src, source_encoding="UTF-8",
                           scope=scope, context=context)
    tree = Parsing.p_module(scanner, True, name, ctx=Parsing.Ctx())
    tree.scope = scope
    return tree


def _stats(node: Any) -> list[Any]:
    s = getattr(node, "stats", None)
    return s if isinstance(s, list) else [node]


def _unwrap(node: Any) -> Any:
    """Strip CReference/CConst wrappers from declarator/type nodes."""
    ref = const = False
    seen = 0
    while node is not None and seen < 8:
        t = type(node).__name__
        if "Reference" in t:
            ref = True
        elif "Const" in t:
            const = True
        elif t not in ("CReferenceDeclaratorNode", "CConstDeclaratorNode"):
            break
        node = getattr(node, "base", None) or getattr(node, "base_type", None)
        seen += 1
    return node, ref, const


def _type_name(node: Any) -> str:
    """Render one type node as the pxd spells it.

    A shape this cannot render RAISES. It used to answer "void" for
    anything without a plain name, which is right for a constructor and
    a silent lie for everything else: a `vector[string]` return came out
    as void, mapped to None, and the codegen emitted a method that
    claimed to return nothing. Unmapped types are already fatal one
    layer up; this is the same rule one layer down."""
    t = type(node).__name__
    if t == "CConstOrVolatileTypeNode":
        return _type_name(node.base_type)
    if t == "TemplatedTypeNode":
        args = ", ".join(_type_name(a) for a in (node.positional_args or []))
        return f"{_type_name(node.base_type_node)}[{args}]"
    if t == "CComplexBaseTypeNode":
        # A template argument that is more than a name: `CValue *` in
        # `vector[CValue *]`, and const/reference spellings of it.
        out = _type_name(node.base_type)
        d = node.declarator
        while d is not None and type(d).__name__ != "CNameDeclaratorNode":
            dt = type(d).__name__
            if dt == "CPtrDeclaratorNode":
                out += "*"
            elif dt == "CReferenceDeclaratorNode":
                out += "&"
            elif dt == "CConstDeclaratorNode":
                out = "const " + out
            else:
                raise ValueError(
                    f"pxd declares a template argument this parser cannot "
                    f"render: {dt}")
            d = getattr(d, "base", None)
        return out
    n = getattr(node, "name", None)
    if n is not None:
        return str(n)
    if t == "CSimpleBaseTypeNode":
        return "void"  # a bare return: a constructor
    raise ValueError(
        f"pxd declares a type this parser cannot render: {t}")


def _func_info(var_node: Any) -> Proto | None:
    bt = var_node.base_type
    for dclr in var_node.declarators:
        d, seen = dclr, 0
        while d is not None and seen < 6:
            if type(d).__name__ == "CFuncDeclaratorNode":
                base, _, _ = _unwrap(getattr(d, "base", None))
                fname = getattr(base, "name", None) or dclr.name
                params = []
                for a in d.args or []:
                    anode, ref, const = _unwrap(a.declarator)
                    pname = getattr(anode, "name", None)
                    tname = _type_name(a.base_type)
                    if ref:
                        tname += "&"
                    if const:
                        tname = "const " + tname
                    params.append((pname, tname))
                return {
                    "name": fname,
                    "params": params,
                    "ret": _type_name(bt),
                    "const": bool(getattr(d, "is_const_method", 0)),
                    "throws": getattr(d, "exception_check", None) == "+",
                }
            d = getattr(d, "base", None)
            seen += 1
    return None


def _is_ctor(func: Proto, class_cython_name: str) -> bool:
    return bool(func["name"] == class_cython_name)


def _bare(type_name: str) -> str:
    """Strip const/reference/pointer decoration from a raw C type."""
    t = type_name.strip()
    if t.startswith("const "):
        t = t[6:]
    return t.rstrip("&*").strip()


def _is_copy_ctor(func: Proto, class_cython_name: str) -> bool:
    """A one-argument constructor taking its own class. Pure binding
    glue - no Python surface ever calls it - so it never reaches the
    overload set."""
    return (len(func["params"]) == 1
            and _bare(func["params"][0][1]) == class_cython_name)


def extract_api(pxd_text: str,
                module_name: str = "c_declarations") -> dict[str, Any]:
    """
    Parse pxd text into:
    {
      'header': str,
      'nogil': bool-per-class dict? (block-level; reported once),
      'classes': {cython_name: {'cpp': quoted cname, 'bases': [cython names],
                                'methods': [{name, params[(n,t)], ret, throws}],
                                'ctors': [[(n, t), ...], ...]}},
      'free_functions': [...]
    }
    Constructors are reported SEPARATELY from methods, never mixed in:
    they share the class's name, so a merged list would carry a method
    called CStorePath. They used to be dropped outright, which left the
    pxd - the only machine-readable record of them, since Cython
    exposes no signature for __cinit__ - unable to type construction at
    all.

    Copy constructors are excluded from the set: they are binding glue
    with no Python surface. Overloads arrive in declaration order.

    Raw C type names are preserved (mapping happens in model.py so this
    module stays free of policy).
    """
    tree = parse_pxd_module(module_name, pxd_text)
    # Three value shapes under one roof - a header string, a dict of
    # classes, a list of free functions - so the top-level value type
    # is Any and each read narrows it.
    api: dict[str, Any] = {"header": None, "classes": {}, "free_functions": []}
    for st in _stats(tree.body):
        if type(st).__name__ != "CDefExternNode":
            continue
        api["header"] = st.include_file
        for e in _stats(st.body):
            t = type(e).__name__
            if t == "CppClassNode":
                methods, ctors = [], []
                for v in e.attributes or []:
                    f = _func_info(v)
                    if f is None:
                        continue
                    if _is_ctor(f, e.name):
                        if not _is_copy_ctor(f, e.name):
                            ctors.append(f["params"])
                        continue
                    methods.append(f)
                api["classes"][e.name] = {
                    "cpp": e.cname,
                    "bases": [b.name for b in (e.base_classes or [])],
                    "methods": methods,
                    "ctors": ctors,
                }
            elif t == "CVarDefNode":
                # A free function inside `cdef extern from ...:` is a
                # CVarDefNode carrying a CFuncDeclaratorNode - the same
                # shape a class method has, one level up. It is NOT a
                # CFuncDefNode; this branch used to look for one of
                # those, so it never matched and the free-function list
                # was silently always empty.
                f = _func_info(e)
                if f is not None:
                    api["free_functions"].append(f)
    return api
