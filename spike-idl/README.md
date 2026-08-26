# Spike: a Python declaration as the source of the binding layer

Proves that `path.pyx`, `path.pxd` and `c_path.pxd` can be GENERATED
from a plain `.py` declaration, by regenerating files that already
exist and swapping them in.

    nix run --file .. ourPython -- emit.py path out
    diff -u ../cythonix-bindings/cythonix_bindings/path.pyx out/path.pyx

## Result

The generated files compiled in place of the hand-written ones.
`nix build --file . cythonix` passed, 159 tests, `nix run --file .
check` clean. Then swapped back - nothing in the three packages
changed.

Ignoring comments and docstring WORDING, the diff is empty. Every
declaration, every marshalling step, every dunder matches.

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

    >>> cxx_of(StorePath)
    TypeError: <class 'StorePath'> carries no C++ spelling. Declare it
    through an Annotated alias in declare.py rather than as a bare type.

A parameter of a BOUND type is the first real gap - `Store.is_valid_path`
genuinely takes a `StorePath` - and it needs the emitter to know how to
unwrap one, which is the same knowledge `_get()` already encodes.

## Not done

No `_cpp` shim generation; path.pyx needs none. No manifest, codec,
surface or build changes. No migration of store.pyx - one module
proves or kills the idea, and this one proves it.
