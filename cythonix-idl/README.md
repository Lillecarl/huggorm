# cythonix-idl

The declarations, and the emitters that read them.

A declaration is one Python file per bound area, in
`src/cythonix_idl/decl/`. It is never RUN. Everything in
`cythonix_bindings` and everything the generator writes above it comes
from what a declaration SAYS.

    decl/path.py       a document, never executed
      |
      +-- read.py      ast.parse -> names, docs, resolved C++ facts
            |
            +-- nbemit.py   -> path.cpp, the nanobind binding
            +-- manifest.py -> the entry every generated surface reads
            +-- pyi.py      -> path.pyi, by transforming the tree
            +-- pyenum.py   -> a vocabulary, as a StrEnum module

Four readings of one document, rather than four stages of a pipeline.
None of them is another's input, so none of them can disagree.

## The declaration never executes

`read.py` uses `ast.parse`. No import, no decorator call, no class
body runs. That is not a purity preference. It buys two things:

- **A declaration can name a C++ type this machine cannot compile.**
  Reading costs a parse, so the manifest is knowable before a compiler
  exists - which is what lets the async wrappers, the protocols, the
  RPC stubs and the type stubs be written without one.
- **A typo is a line number.** `read.py` refuses a name it cannot
  resolve and points at the line:

      read.DeclarationError: line 64: 'str' is not from declare.
      A declaration may only use the vocabulary it imported.

`declare.py` DOES execute, and earns it. A decorator's job is to write
a field on a `Decl`, so rather than restate that mapping, `read.py`
applies the real decorator to a throwaway object and reads what was
written. `header` sets `.header` in exactly one place, and a decorator
that gains an argument needs no edit in the reader.

## How the facts split

**Annotated aliases carry facts about a TYPE.** `StrView` is
`std::string_view` wherever it appears, written once in `declare.py`
and read by name. A per-method `Annotated[str, Cxx("string_view")]`
says the same thing and drowns the signature it describes.

**Decorators carry facts about a METHOD or CLASS.** `@cxx_name`,
`@header`, `@binding`, `@wire_value`, `@blocks`.

The rule between the declaration and the emitter: the declaration
states what C++ IS, the emitter states what crossing costs. A view
must not outlive the object it points into - that is a fact about the
Python boundary, not about `nix::StorePath`, so no declaration
mentions it. `Cxx(copy="view")` on the alias triggers the copy, once,
for every method that returns one.

## The escape hatches, and the count

`@cxx_body(source)` carries C++ one method cannot be derived into.
`@custom(name, source)` carries C++ a whole class needs and no method
owns. Both are counted, and the build prints the ratio per class:

    decl/path.py -> .../path.cpp (module cythonix_bindings.path): StorePath
      StorePath: 10 derived
    decl/store.py -> .../store.cpp (...): PathInfo, StoreLocation, Store
      PathInfo: 9 derived
      StoreLocation: 2 derived
      Store: 8 derived, 10 hatched (39 lines)

Same bargain as `_cpp/README`: a hatch nobody measures becomes the
place the real code lives. A number in a build log is cheaper than a
review that has to notice.

Zero for `decl/path.py`, which is why that declaration is the one to
read first. `Store` is the other end, and its own docstring says why:
rendering a store path against a store directory is a decision, not a
binding.

## It is text, not `ast.unparse`

`nbemit.py` builds C++ lines. `cgen` is the only real "C++ AST from
Python" library and it models DECLARATIONS - `Struct`,
`FunctionDeclaration` - while its expressions are strings. A nanobind
module body is almost entirely one expression, so cgen hands back
structure for the part we do not need and strings for the part we do.

`pyi.py` and `pyenum.py` are the other shape, and for the opposite
reason: their output is Python and the declaration is already Python,
so they TRANSFORM the declaration's own tree and unparse it. A string
builder can emit a file that does not parse; `ast.unparse` cannot.

What replaces the safety net for C++ is the compiler. An emitter that
spells a type wrong does not write a bad binding that imports - it
fails to build.

## Gates

    nix run --file . spike

`gates/nbcheck.py` emits every declaration and diffs the result
against the hand-written nanobind in `~/Code/nanopynix`, binding by
binding. Where the two disagree it says which one is right and why -
`nb::is_operator()` on a comparison, an ordering the declaration asks
for, a view returned by method pointer. It is skipped with a reason on
a machine without that checkout.

`decl/pathinfo.py`, `decl/nixstore.py` and `decl/storefns.py` exist
only for that gate. The declarations the BUILD reads are listed in
`generate.py`.

## History

`tasks/053-a-declaration-as-the-source-of-the-bindings.md.done` is the
spike report this package grew out of, kept as written. It measures a
Cython emitter that no longer exists.
