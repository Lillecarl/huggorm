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

from Cython.Compiler import Parsing
from Cython.Compiler.Scanning import PyrexScanner
from Cython.Compiler.TreeFragment import StringParseContext, StringSourceDescriptor

# No type table here on purpose: this module reports the raw C names and
# model.py owns the mapping, so the two cannot drift apart.


def parse_pxd_module(name: str, text: str):
    context = StringParseContext(name)
    scope = context.find_module(name, need_pxd=False)
    src = StringSourceDescriptor(name, text)
    scanner = PyrexScanner(StringIO(text), src, source_encoding="UTF-8", scope=scope, context=context)
    tree = Parsing.p_module(scanner, True, name, ctx=Parsing.Ctx())
    tree.scope = scope
    return tree


def _stats(node):
    s = getattr(node, "stats", None)
    return s if isinstance(s, list) else [node]


def _unwrap(node):
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


def _type_name(node) -> str:
    t = type(node).__name__
    if t == "CConstOrVolatileTypeNode":
        return _type_name(node.base_type)
    n = getattr(node, "name", None)
    if n is None:
        return "void"  # bare return / untyped
    return n


def _func_info(var_node):
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


def _is_ctor(func: dict, class_cython_name: str) -> bool:
    return func["name"] == class_cython_name


def extract_api(pxd_text: str, module_name: str = "c_declarations") -> dict:
    """
    Parse pxd text into:
    {
      'header': str,
      'nogil': bool-per-class dict? (block-level; reported once),
      'classes': {cython_name: {'cpp': quoted cname, 'bases': [cython names],
                                'methods': [{name, params[(n,t)], ret, throws}]}},
      'free_functions': [...]
    }
    Ctors and copy-ctors are dropped; they are binding-layer concerns.
    Raw C type names are preserved (mapping happens in model.py so this
    module stays free of policy).
    """
    tree = parse_pxd_module(module_name, pxd_text)
    api = {"header": None, "classes": {}, "free_functions": []}
    for st in _stats(tree.body):
        if type(st).__name__ != "CDefExternNode":
            continue
        api["header"] = st.include_file
        for e in _stats(st.body):
            t = type(e).__name__
            if t == "CppClassNode":
                methods = []
                for v in e.attributes or []:
                    f = _func_info(v)
                    if f is None or _is_ctor(f, e.name):
                        continue
                    methods.append(f)
                api["classes"][e.name] = {
                    "cpp": e.cname,
                    "bases": [b.name for b in (e.base_classes or [])],
                    "methods": methods,
                }
            elif t == "CFuncDefNode":
                # free functions arrive as CFuncDefNode with direct fields
                args = []
                for a in e.args or []:
                    anode, ref, const = _unwrap(a.declarator)
                    tname = _type_name(a.base_type)
                    if ref:
                        tname += "&"
                    if const:
                        tname = "const " + tname
                    args.append((getattr(anode, "name", None), tname))
                api["free_functions"].append(
                    {
                        "name": e.name,
                        "params": args,
                        "ret": _type_name(e.return_type_node) if e.return_type_node is not None else "void",
                    }
                )
    return api
