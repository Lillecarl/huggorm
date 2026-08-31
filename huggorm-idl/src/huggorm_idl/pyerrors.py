"""Declaration -> the exception module, and the translator's catch chain.

Two outputs from one file, and they used to be two hand-written files
that had to agree. A Python class with no catch clause could never be
raised; a catch clause naming a class the module did not define failed
at import. Nothing checked either way.

The module is a TRANSFORM, like `pyenum.py`: a declaration of
exception classes IS the module, once the `cxx = "nix::..."` lines
come off. Every docstring, every base and the order are the
declaration's own nodes.

The chain is DERIVED, and the thing it derives is the part a person
gets wrong. C++ picks the FIRST matching catch, so a base listed
before its subclass swallows it - `nix::Error` first would make every
one of these a NixError. Python's own inheritance already says which
class derives from which, so the order is computed rather than
maintained.
"""

import ast

from huggorm_idl.read import Module

# The attribute a declared exception uses to name its C++ class. Not a
# decorator: an exception declaration has no behaviour to mark, and a
# bare assignment reads as the fact it is.
CXX = "cxx"


def _cxx_of(node: ast.ClassDef) -> str:
    """The C++ class this exception stands for, from its `cxx` line."""
    for item in node.body:
        if (isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
                and item.targets[0].id == CXX
                and isinstance(item.value, ast.Constant)):
            return str(item.value.value)
    return ""


def _bases(tree: ast.Module) -> dict[str, str]:
    """Each declared class to its declared base, by name."""
    return {n.name: (n.bases[0].id if n.bases
                     and isinstance(n.bases[0], ast.Name) else "")
            for n in tree.body if isinstance(n, ast.ClassDef)}


def _depth(name: str, bases: dict[str, str]) -> int:
    """How far this class is from the root of the declared hierarchy.

    The sort key for the catch chain. A subclass is deeper than its
    base, so ordering by depth descending puts every class before
    anything it derives from - which is what C++ needs, because it
    takes the first catch that matches."""
    seen, depth = set(), 0
    while name in bases and bases[name] and name not in seen:
        seen.add(name)
        name, depth = bases[name], depth + 1
    return depth


def chain(tree: ast.Module, raise_as: str) -> list[str]:
    """The translator's catch chain, most-derived first.

    `raise_as` is the C++ helper that sets the Python error: it is the
    one part of this that is not derived, because turning a
    `std::exception` into a live Python exception is nanobind's
    protocol rather than anything a declaration knows.

    A class with no `cxx` is skipped. That is how a Python-only
    exception - one this binding raises itself and Nix never throws -
    stays in the module without inventing a catch for it.
    """
    bases = _bases(tree)
    caught = [(n.name, _cxx_of(n)) for n in tree.body
              if isinstance(n, ast.ClassDef) and _cxx_of(n)]
    caught.sort(key=lambda pair: -_depth(pair[0], bases))
    out = ["    try {", "        throw;"]
    for name, cxx in caught:
        out.append(f"    }} catch (const {cxx} & e) {{")
        out.append(f'        {raise_as}("{name}", e);')
    out.append("    }")
    return out


def module(mod: Module, tree: ast.Module, doc: str) -> str:
    """The exception module, as the declaration with its C++ taken off.

    A transform rather than a print, for `pyenum.py`'s reason: the
    output is Python and the declaration already is. What changes is
    only what a caller must not see - the `cxx` lines, which name C++
    that no Python caller can act on.
    """
    body: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            node.body = [item for item in node.body
                         if not (isinstance(item, ast.Assign)
                                 and len(item.targets) == 1
                                 and isinstance(item.targets[0], ast.Name)
                                 and item.targets[0].id == CXX)]
            node.decorator_list = []
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            # The module docstring, replaced below.
            continue
        body.append(node)
    header = ast.Expr(value=ast.Constant(value=doc))
    out = ast.Module(body=[header, *body], type_ignores=[])
    ast.fix_missing_locations(out)
    return ast.unparse(out)


def declared(mod: Module) -> list[str]:
    """Every exception class this declaration names, for a gate."""
    return [cls.name for cls in mod.classes]
