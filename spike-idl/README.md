# Spike: a Python declaration as the source of the binding layer

One `.py` file that is never RUN. Everything below it is generated
from what it says.

    nix run --file .. ourPython -- check.py --manifest <built manifest.json>

`check.py` emits from `path.py` and diffs the result against what the
repo already has - two artefacts, produced two different ways.

## The two claims

### 1. The Cython comes from the declaration

`path.pyx`, `path.pxd` and `c_path.pxd`, generated and swapped in
place of the hand-written ones. `nix build --file . cythonix` passed,
159 tests passed, `nix run --file . check` clean. Swapped back;
nothing in the three packages changed.

Ignoring comments and docstring WORDING, the emitted code is
identical but for one local variable name: the human wrote `c_name`
where the emitter derives `c_base_name` from the parameter. That is
the whole diff, and it compiles.

### 2. The MANIFEST comes from the same declaration

This is the one that decides whether the idea is worth adopting.

`model.py` builds the manifest by IMPORTING `cythonix_bindings` and
reflecting on compiled extension types: `getattr(cls, "_threading")`,
`cls.__dict__["_binds"]`, `getattr(cls, d) is not getattr(object, d)`
for each of the nine value dunders. So the build has one possible
order - C++ compiles, Cython compiles, the manifest is learnt - and
four surfaces (async, protocols, RPC, stubs) wait behind a C++
compiler for facts a human decided in a `.pyx` before any of it began.

`manifest.py` derives that same entry from the declaration.

    StorePath:     17 of 17 fields agree
    PathInfo:      15 of 17
    StoreLocation: 16 of 17

For StorePath, all seventeen. `_binds` is "C" plus the class name. `_threading` is
what `@binding` said. The nine dunders are what `@wire_value`
implies: a value compares, hashes and prints; `order=True` adds the
four `functools.total_ordering` fills in; `text=` adds `__str__`.

Reflection was reading back a fact written down two files earlier.

### 3. Where they disagree, the declaration is right

The four disagreements are all bugs in the reflected manifest, and
one of them ships.

**The stubs promise an order that raises** (tasks/052). `PathInfo`
and `StoreLocation` are stubbed with `__lt__`, `__le__`, `__gt__`
and `__ge__`. Neither supports any of them:

    >>> a < b
    TypeError: '<' not supported between instances of
    'cythonix_bindings.store.PathInfo' and ...

`model.py` asks `getattr(cls, "__lt__") is not object.__lt__`, and a
cdef class defining ANY rich comparison gets all six slots filled by
CPython. So the test answers True for a comparison that does not
exist. StorePath passes only by luck: it really is ordered, so the
right answer and the wrong measurement agree.

Reflection measures what the COMPILER emitted. The declaration says
what the author meant, and `order=True` is the whole of it.

**`deriver` contradicts its own entry.** The reflected return type is
`StorePath`, while the same entry's `wire_fields` say `StorePath?`
and the docstring documents returning None. The hand-written pyx
annotates it wrong; the declaration says `StorePath | None` and the
wire spelling follows from it.

**`ca` reflects as `typing.Union[str, None]`** where the source said
`str | None` - harmless, and a second spelling of one type is still
a second spelling.

Each is pinned in `check.py` on BOTH values, so a reflected entry
that changes stops being excused and fails.

## The declaration never executes

`read.py` uses `ast.parse`. No import, no decorator call, no class
body runs. That is not a purity preference, it buys three things:

- **A declaration can name a C++ type this machine cannot compile.**
  Reading costs a parse, so the manifest is knowable before a
  compiler exists, which is what breaks the ordering above.
- **The shim layer disappears.** Making a declaration IMPORTABLE
  needed `cyshims.py` to stand in for `cython.cimports`, plus a
  canary asserting Cython had not moved underneath it. Both now live
  in `probe/`, as evidence about pure mode rather than as build
  dependencies.
- **A typo is a line number.** `read.py` refuses a name it cannot
  resolve and points at the line:

      read.DeclarationError: line 64: 'str' is not from declare.
      A declaration may only use the vocabulary it imported.

`declare.py` still executes, and earns it. A decorator's job is to
write a field on a `Decl`, so rather than restate that mapping,
`read.py` APPLIES the real decorator to a throwaway object and reads
what was written. `header` sets `.header` in exactly one place, and a
decorator that gains an argument needs no edit in the reader.

## The shape this suggests

    path.py            a document, never executed
      |
      +-- read.py      ast.parse -> names, docs, resolved C++ facts
            |
            +-- emit.py       -> c_path.pxd, path.pxd, path.pyx
            +-- manifest.py   -> the wrapper entry the surfaces read

Two readings of one document, rather than two stages of a pipeline.
The `.pyx` and the manifest cannot disagree, because neither is the
other's input.

## Emitting found four bugs a template would have shipped

Each one is a place where the emitted code has to differ from what the
declaration says, and each was wrong in the first attempt:

- the pyx called `hashPart()`. The pxd alias IS the mapping -
  `hash_part "hashPart"` means Cython code says `hash_part` - so the
  C++ name does not compile there.
- the constructor read `string base_name`. A pyx signature is the
  PYTHON surface; the C++ spelling belongs in the pxd.
- `inspect.getdoc` dedents, so every continuation line landed flush
  against the left margin.
- a stray space where a method needed no alias.

## The five questions

### 1. Can Annotated carry the C++ facts without becoming unreadable?

Yes, by not carrying most of them. The split that works:

**Annotated aliases carry facts about a TYPE.** `StrView` is
`std::string_view` wherever it appears, written once in `declare.py`
and read by name. A per-method `Annotated[str, Cxx("string_view")]`
says the same thing and drowns the signature it describes.

**Decorators carry facts about a METHOD or CLASS.** `@cxx_name`,
`@header`, `@binding`, `@wire_value`. A plain `def` takes a decorator,
which is exactly what a cdef method cannot - and the reason the
current bindings put class markers in the class body.

Read `path.py` and judge: it is shorter than the pyx it produces and
says more.

### 2. Does the produced-value pattern come from the declaration?

Partly, and the gap is the honest finding.

The emitter derives the whole ownership shape - the `_ptr` field, the
`_get()` NULL guard, `__copy__`/`__deepcopy__`, `__dealloc__`, and the
`__init__`-not-`__cinit__` split - from one fact: the class binds a
C++ type through a pointer. StorePath needs no declaration for any of
it.

What it does NOT do is the PRODUCED form: a value whose `__init__`
raises and which another object builds through `__new__` and assigns
into. `_round_trip` refuses when the declared fields do not map onto
constructor parameters, with the reason, rather than emitting the
constructible form for a class that has no constructor. PathInfo and
StoreLocation are both that shape, so this is the next thing to build
- and it is a shape, not a special case.

### 3. Where does the `_view` copy rule live?

The emitter owns it, and that is the right side of the line.

The declaration says `to_string` returns a `string_view`. That a view
must not outlive the object it points into is a fact about the
PYTHON BOUNDARY, not about nix::StorePath - so no declaration should
have to remember it, and none does: `Cxx(copy="view")` on the alias is
what triggers the copy, once, for every method that returns one.

The general rule this suggests: the declaration states what C++ IS,
the emitter states what crossing costs.

### 4. What is the escape hatch, and how is it counted?

`@custom(name, source)` carries verbatim Cython, and `emit.py` prints
how many lines went through it on every run:

    emitted 3 file(s) for StorePath; 0 line(s) came through the custom hatch

Zero for path.py, which is the point of choosing path.py first.

Same bargain as `_cpp/README`: a hatch nobody measures becomes the
place the real code lives. Printing the count on every build makes
growth visible rather than gradual, and a number in a build log is
cheaper than a review that has to notice.

### 5. Does `blocking=True` emit `with nogil:`?

Yes. `store.py` declares `is_valid_path` and gets:

    def is_valid_path(self, bint path) -> bint:
        cdef bint out
        with nogil:
            out = self._get().is_valid_path(path)
        return out

It is NOT compiled, and the declaration says why: nix::Store is
abstract and opened by a URI, so a real Store needs a factory
constructor and a `shared_ptr`, neither of which this emitter writes.
`blocks` also exists per-method, because a class whose calls mostly
block still has accessors that cannot.

## Two findings beyond the five

### Prose belongs in the declaration, not in the output

`c_path.pxd` carries paragraphs of WHY - the deleted default
constructor, why validation lives in C++ - and the first instinct was
to add `note=` plumbing so the generator could copy them out.

That is backwards. Nobody edits generated code, so prose ABOUT A
DECLARATION belongs in the declaration file, as ordinary Python
comments, where the person who will change it is already reading. Only
prose about RUNTIME behaviour - docstrings - has to survive the
crossing, and those do.

`path.py` now carries that prose as comments. The generated pxd is
shorter and says "read the declaration".

### It is text, not `ast.unparse`, and that is not a choice

`emitter.py` builds Python with `ast` and renders it with
`ast.unparse`, which makes emitting code that does not parse
impossible. That guarantee is not available here: a `.pyx` is not
Python. `cdef class`, `cdef string c_name`, `except NULL` and
`with nogil:` have no ast node, and Cython ships a parser but no
unparser.

So this builds lines. The discipline it keeps instead is that every
line comes from a small named builder and nothing concatenates a
caller's string into a template - but the safety net is weaker, and
the four bugs above are what that costs. Worth knowing before
committing to the direction: the acceptance test cannot be "it
parses", it has to be "it compiles", which means the build is the
only real gate.

## What it refuses

A shape it cannot emit stops with a reason rather than guessing:

    read.DeclarationError: line 64: 'str' is not from declare. A
    declaration may only use the vocabulary it imported.

A parameter of a BOUND type is the first real gap - `Store.is_valid_path`
genuinely takes a `StorePath` - and it needs the emitter to know how to
unwrap one, which is the same knowledge `_get()` already encodes.

## Not done

No `_cpp` shim generation; path.pyx needs none. No codec, surface or
build changes - `manifest.py` emits an entry and diffs it, it does not
feed the real generator. No migration of store.pyx: one module proves
or kills the idea, and this one proves it.

`emit.py` writes no PRODUCED value yet. `PathInfo` and
`StoreLocation` hold no C++ at all - object slots something else
fills, `_from_parts` through `__new__`, an `__init__` that refuses -
and `_round_trip` says so rather than emitting the constructible
form. The manifest half covers them; the Cython half does not.

That is why store.py's Cython check prints SKIPPED rather than
passing: both classes live inside the repo's `store.pyx` beside
`Store`, which the emitter also cannot write.
