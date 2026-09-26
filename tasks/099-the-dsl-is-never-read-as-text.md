# The DSL is never read as text

**OPEN.** Carl, 2026-09-26:

> The Python DSL should never be string parsed at-all, emission can be
> more liberal but the DSL should never be. Preferably as much of the
> Python -> Python code should be AST transformations as-well (where
> feasible).

> Quoting types should be avoided indeed, at runtime the import
> annotation from __future__ [...] to do automatic defering in the
> emitted Python.

## Done

- Declarations write annotations unquoted (Python 3.14 defers them).
- `read.type_of` resolves each annotation from the imported object
  through `annotationlib`, and `Type` carries its structure (`origin`,
  `args`). A quoted annotation and an unimported name are refused.
- The cppgen emitters walk `Type`'s structure. The emitted C++ and
  the generated package stayed byte-identical.
- `cppgen/pyi.py` was dead and is deleted.

## Still read as text

Each is a place the reader or an emitter parses a string the
declaration wrote, or re-parses one the build wrote.

1. **`Field` types.** `Field("base_name", "str", read="to_string")`
   names a type as a string, and `nbemit` tests `f.type.startswith(
   "list[")` (two sites). The accessor already carries a `Type`.
2. **`@produced(by="Store.read_derivation")`.** A dotted string names
   a method. The method object is importable.
3. **Defaults.** `Param.default` is `ast.unparse` of the default, and
   `nbemit._default` reads it back with `ast.literal_eval`. The
   imported function's `__defaults__` hold the values.
4. **The manifest boundary.** The manifest carries type strings, and
   `pygen` (`wiretypes`, `model`) parses them again. Carl chose to
   leave this for now (reader and cppgen first).
5. **Decorators replayed on stand-ins.** `_class` and `_method` apply
   the AST's decorators to fresh stand-in objects, although the
   import has already applied them to the real ones.
6. **Emitted Python quotes types.** Emit `from __future__ import
   annotations` instead (Carl).

## Found on the way

`nix run --file . check` prints `declaration -> nanobind: SKIPPED, no
~/Code/nanopynix`. nanopynix lives in the nixidae umbrella now, so
`gates/nbcheck.py` has not run on this machine. It should find
nanopynix through the umbrella, not a fixed path.
