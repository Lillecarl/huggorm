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

One fact can decide many lines, and that is the test of whether it
belongs in the vocabulary. `@binding(via="get()")` says the bound C++
type is a HANDLE - `cythonix::Bridge` roots a GC-resident value and
hands it over through `get()`. From that one word the emitter derives
every call through the handle, every return wrapped back into it and
every parameter unwrapped out of it: twenty verbatim lines before it
existed, and none after.

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
    decl/eval.py -> .../eval.cpp (...): Value, EvalState
      Value: 10 derived, 2 hatched (2 lines)
      EvalState: 12 derived

Same bargain as `_cpp/README`: a hatch nobody measures becomes the
place the real code lives. A number in a build log is cheaper than a
review that has to notice.

Zero for `decl/path.py`, which is why that declaration is the one to
read first. `Store` is the other end: a `dynamic_cast` to reach a
local store, a source accessor to add a path, a store directory
joined onto a path - each of those is a decision rather than a
binding, and each is counted.

The number is a lever, not a score. Every time it dropped, the reason
was a fact that belonged in the vocabulary: `Value` fell from eleven
hatched lines to two the moment `via` existed. A count that stays
high on one class is that class telling you it is doing something
real.

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

`gates/nbcheck.py` emits every declaration and compares the result,
binding by binding, against the hand-written nanobind in
`~/Code/nanopynix`.

That corpus is NOT the reference and matching it is NOT the goal.
It is hand-written, so it is inconsistent the way hand-written code
is - `nb::is_operator()` on some comparisons and not others, `__lt__`
bound nowhere, a lambda where a method pointer would do. The emitter
is meant to be BETTER. The only reason to read it is that it is real:
tested nanobind over the same library, written by a person solving
the same problems, and not in this repo - so it says something the
build cannot say about itself.

Where the two agree there is no question. Where they differ, one of
them is wrong, and the gate makes somebody say which: COSMETIC,
BETTER (with the evidence pinned, so the emitter cannot regress into
agreement), or DIVERGENT. An unjudged difference fails - and the
answer is not always "we are right". A binding the corpus has and the
declaration cannot express is a gap in the vocabulary, and that is
the direction worth mining.

It is skipped with a reason on a machine without that checkout.

`decl/pathinfo.py`, `decl/nixstore.py` and `decl/storefns.py` exist
only for that gate. The declarations the BUILD reads are listed in
`generate.py`.

## History

`tasks/053-a-declaration-as-the-source-of-the-bindings.md.done` is the
spike report this package grew out of, kept as written. It measures a
Cython emitter that no longer exists.
