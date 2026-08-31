"""The emitters. One declaration set, two backends.

A declaration says what a Nix class IS. What that MEANS for an output
is decided here, and there are two outputs far enough apart to be
worth naming:

- `cppgen` fills `huggorm_bindings`: the nanobind C++, and the stubs,
  enum modules and exception module that ship beside the compiled
  extension.
- `pygen` fills `huggorm_generated`: the async wrappers, the
  protocols, the RPC client and the wire schema.

They are ONE package, not two, because they read one IR from one
reader. A package boundary between them would say the split is
architectural, and it is not: `pygen` still reflects on the compiled
extension for enums, errors and free functions, and that seam is
being closed rather than kept. Two distributions would have made it
permanent.

`payload` is neither. It is hand-written Python that SHIPS into the
emitted package - the thread runners, and the wire vocabulary the
codec reads at run time. It sits here because `pygen` copies it, not
because `pygen` runs it.
"""
