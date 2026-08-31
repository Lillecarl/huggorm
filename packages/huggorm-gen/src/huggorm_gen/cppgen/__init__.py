"""The emitters. Declarations in, generated source out.

Each one reads the same parsed declaration and none knows about the
others:

- `nbemit.py` writes nanobind C++ - the binding itself.
- `manifest.py` writes the manifest entry every generated Python
  surface above the bindings is built from.
- `pyi.py` writes the type stub, by transforming the declaration's
  own tree.
- `pyenum.py` writes a vocabulary as a StrEnum module, the same way.
- `pyerrors.py` writes the exception module and the C++ catch chain
  that raises it.

Nothing here says what is declared. `huggorm_decl` owns the
documents and the lists naming them, and `huggorm_dsl` owns the
language they are written in and the reader that parses one. This
package is handed a declaration and decides what it means for one
output.
"""
