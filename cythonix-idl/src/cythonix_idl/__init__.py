"""Declarations, and the emitters that read them.

One document per bound area, in `decl/`. Nothing in there is ever
executed: `read.py` parses it with `ast.parse`, so a declaration can
name a C++ type this machine has never compiled.

Three emitters read the same parsed declaration and none knows about
the others:

- `emit.py` writes Cython - a `.pxd` pair and a `.pyx`, plus the C++
  shim underneath them.
- `nbemit.py` writes nanobind C++.
- `manifest.py` writes the manifest entry every generated Python
  surface above the bindings is built from.

It was `spike-idl/` while the question was whether this works. The
build compiles what it emits now, and `cythonix_bindings.path` has no
hand-written source at all, so it is a package.
"""

import pathlib


def declaration(name: str) -> str:
    """One declaration by name, as a path its reader can open.

    Declarations are DATA to this package: `read.py` parses them, so
    nothing imports them and `import cythonix_idl.decl.path` would be
    the wrong shape as well as an execution nobody wants.

    Named through here rather than found relative to a caller's own
    file, because there are three callers in two directories and each
    one guessing its way back to `decl/` is three chances to guess
    wrong."""
    here = pathlib.Path(__file__).resolve().parent
    return str(here / "decl" / (name if name.endswith(".py")
                                else f"{name}.py"))
