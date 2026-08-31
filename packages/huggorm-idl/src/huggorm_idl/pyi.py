"""Declaration -> type stub, by transforming the tree.

`nbemit.py` builds TEXT, and for C++ it has to: `cgen` models C++
declarations while a nanobind module body is one expression. There is
no tree to transform, so a string builder is not a shortcut there, it
is the only shape available.

A `.pyi` is different. It is PYTHON, and the declaration is already
Python - `def to_string(self) -> StrView: ...` with a docstring and an
empty body is nearly the stub line for line. So this does not print a
stub. It edits the declaration's own tree and unparses it:

- the vocabulary decorators come off, because they said what the
  binding does rather than what a caller sees;
- an annotation resolves through the vocabulary, `StrView` to `str`,
  because a view is copied before it reaches Python;
- the markers and the value dunders go in, since those are what the
  declaration IMPLIES rather than what it wrote.

Everything else is left exactly as the declaration wrote it. The
docstrings are not re-indented, the parameter names are not rebuilt,
the return annotations are not looked up in a table - they are the
declaration's own nodes, moved.

What that buys is the failure mode. A string builder can emit a stub
that does not parse, and nothing notices until a typechecker reads
it. `ast.unparse` cannot: whatever comes out is a tree that was valid
before it was printed.
"""

import ast

from huggorm_dsl.read import BUILTIN_DECORATORS, Class, Module
from huggorm_idl.manifest import PYTHON, dunders

# The dunder signatures a value type gets. Not derived from anything
# in the declaration, because they are Python's own protocol: `__eq__`
# takes an object and answers a bool wherever it appears.
DUNDER_SIGNATURE = {
    "__eq__": ("object", "bool"), "__ne__": ("object", "bool"),
    "__lt__": ("object", "bool"), "__le__": ("object", "bool"),
    "__gt__": ("object", "bool"), "__ge__": ("object", "bool"),
    "__hash__": (None, "int"), "__repr__": (None, "str"),
    "__str__": (None, "str"),
}

# The class-body markers a binding carries. Their VALUES are a fact
# about one class; that each is a `str` is a fact about all of them.
MARKERS = ("_threading", "_wire", "_binds")


def _resolve(node: ast.expr | None, vocab: dict[str, str]) -> ast.expr | None:
    """One annotation, as a caller sees it.

    A vocabulary name carries a C++ spelling, and `manifest.PYTHON`
    already maps that onto the Python type - one table, read by the
    manifest and by this. Anything else is left alone: `StorePath` is
    `StorePath` to a typechecker too."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # A declaration quotes a type it cannot import - `"StorePath"`
        # names C++ this machine may never have compiled. A stub has
        # no such problem: it is read, not run, and the name is right
        # there. So the quotes come off rather than being reprinted.
        return ast.Name(id=node.value, ctx=ast.Load())
    if isinstance(node, ast.Name) and node.id in vocab:
        from huggorm_dsl.read import type_of
        t = type_of(node, vocab)
        if t.cxx is not None and t.cxx.spelling in PYTHON:
            return ast.Name(id=PYTHON[t.cxx.spelling], ctx=ast.Load())
    return node


def _method(node: ast.FunctionDef, vocab: dict[str, str]) -> ast.FunctionDef:
    """One declared method, as the stub declares it.

    A copy, and a shallow one on purpose: the docstring and the
    parameter names are the declaration's own nodes."""
    out = ast.FunctionDef(
        name=node.name,
        args=ast.arguments(
            posonlyargs=[],
            args=[ast.arg(arg=a.arg, annotation=_resolve(a.annotation, vocab))
                  for a in node.args.args],
            vararg=None, kwonlyargs=[], kw_defaults=[], kwarg=None,
            defaults=list(node.args.defaults)),
        body=list(node.body),
        # Python's own decorators survive - `@property` and
        # `@overload` are part of the surface a typechecker reads.
        # The vocabulary's do not: they said how to BUILD the binding.
        decorator_list=[d for d in node.decorator_list
                        if isinstance(d, ast.Name)
                        and d.id in BUILTIN_DECORATORS],
        returns=_resolve(node.returns, vocab),
        type_params=[])
    return out


def _dunder(name: str) -> ast.FunctionDef:
    """One value dunder, from Python's protocol rather than the
    declaration. `...` for a body, like every other stub method."""
    takes, gives = DUNDER_SIGNATURE[name]
    args = [ast.arg(arg="self")]
    if takes is not None:
        args.append(ast.arg(arg="other",
                            annotation=ast.Name(id=takes, ctx=ast.Load())))
    return ast.FunctionDef(
        name=name,
        args=ast.arguments(posonlyargs=[], args=args, vararg=None,
                           kwonlyargs=[], kw_defaults=[], kwarg=None,
                           defaults=[]),
        body=[ast.Expr(value=ast.Constant(value=Ellipsis))],
        decorator_list=[],
        returns=ast.Name(id=gives, ctx=ast.Load()),
        type_params=[])


def _marker(name: str) -> ast.AnnAssign:
    return ast.AnnAssign(target=ast.Name(id=name, ctx=ast.Store()),
                         annotation=ast.Name(id="str", ctx=ast.Load()),
                         value=None, simple=1)


def class_stub(cls: Class, node: ast.ClassDef,
               vocab: dict[str, str]) -> ast.ClassDef:
    """One declared class, as a stub declares it."""
    body: list[ast.stmt] = []
    if cls.doc:
        # The class docstring, verbatim. `read.py` keeps it raw and a
        # stub carries the same text the source had.
        body.append(ast.Expr(value=ast.Constant(value=cls.doc)))
    body += [_marker(m) for m in MARKERS if m == "_threading"
             or (m == "_wire" and cls.decl.wire)
             or (m == "_binds" and not cls.is_value)]
    methods = {n.name: n for n in node.body
               if isinstance(n, ast.FunctionDef)}
    if "__init__" in methods:
        body.append(_method(methods["__init__"], vocab))
    body += [_dunder(d) for d in dunders(cls.decl)]
    body += [_method(methods[m.name], vocab) for m in cls.methods
             if m.name in methods]
    return ast.ClassDef(name=cls.name, bases=[], keywords=[], body=body,
                        decorator_list=[], type_params=[])


def stub(mod: Module, tree: ast.Module, doc: str) -> str:
    """A whole `.pyi`, unparsed from the tree it was edited into."""
    classes = {n.name: n for n in tree.body if isinstance(n, ast.ClassDef)}
    out = ast.Module(
        body=[ast.Expr(value=ast.Constant(value=doc)),
              *(class_stub(c, classes[c.name], mod.vocabulary)
                for c in mod.classes if c.name in classes)],
        type_ignores=[])
    return ast.unparse(ast.fix_missing_locations(out)) + "\n"
