# Could Cython's pure Python mode replace the emitted .pyx?

Carl asked. Investigated against Cython 3.2.5's source and, where the
source was ambiguous, by compiling. Every claim below was compiled and
run; the probe is small enough to redo in ten minutes.

**Short answer: pure mode can replace the `.pyx`. It replaces neither
`.pxd`, and that is where the duplication actually lives. But it buys
back something the current spike gives up, and that is worth a
decision rather than a dismissal.**

## What pure mode does, verified by compiling

A `.py`, cythonized with `language="c++"`, produced a real extension
type against a C++ class with a deleted default constructor, a
throwing constructor and a `string_view` accessor:

- `@cython.cclass` gives a real extension type - no `__dict__`,
  confirmed by `t.other = 1` raising AttributeError.
- A C++ member comes from a class annotation: `_ptr:
  shared_ptr[CThing]` emitted `std::shared_ptr<probe::Thing> _ptr;`
  into the struct.
- `cython.cimports.<module>` reaches pxd declarations.
- `cython.operator.dereference` works, and C++ `operator==` through it
  returned the right answers.
- `with cython.nogil:` emitted a real `Py_UNBLOCK_THREADS`.
- `@cython.cfunc` gave a real cdef method, callable from a `def` one.

So the language is there. This is not a toy subset.

## The four things it cannot do

**1. There is no pure-mode spelling of `cdef extern from`.** Nothing
in `Cython/Shadow.py` declares an external header, and Cython's own
test suite has ZERO pure-mode tests that use C++ at all - checked by
grepping every `tests/run/*.py` for `libcpp` and `language=c++`. Every
C++ declaration stays in a `.pxd`.

**2. A sibling module cannot reach a pure-mode cclass without a
`.pxd`.** Compiling a second module that cimports the first fails
with:

    user.py:2:0: 'thing.pxd' not found

This repo hits that immediately: `path.pxd` exists precisely because
`store.pyx` reaches `StorePath._ptr`.

**3. Once that `.pxd` exists, it must also declare every `cfunc`:**

    thing.py:18:4: C method 'slow' not previously declared in
    definition part of extension type 'Thing'

Which is the same duplication a `.pyx` + `.pxd` pair has. Pure mode
does not reduce it - it moves it.

**4. `new` and bare `NULL` are not valid Python.** `new CThing(name)`
compiles under Cython and is a `SyntaxError` to Python:

    self._ptr = new CThing(c_name)
                    ^^^^^^
    SyntaxError: invalid syntax

`NULL` is guarded by `not s.in_python_file` in `Parsing.py:846`. So a
pure-mode binding must allocate through `make_shared` or
`make_unique` - function calls, which stay valid Python - and hold a
smart pointer rather than a raw one. That is an ownership decision
forced by SYNTAX, which is a poor reason to make one, though
`shared_ptr` is arguably the better choice anyway.

There is a fifth, smaller: a pure-mode file that cimports a user pxd
PARSES as Python but does not IMPORT as Python. `Cython.Shadow`'s
cimport mock raises `AttributeError: __spec__`. So it cannot be read
by `import` the way the IDL spike reads its declarations.

## What this means for the three files

For a module like `path`, three files stay three files:

| file | today | under pure mode |
| --- | --- | --- |
| `c_path.pxd` | hand-written | hand-written - no pure spelling exists |
| `path.pxd` | hand-written | hand-written, and now lists cfuncs too |
| `path.pyx` | hand-written | `path.py`, pure mode |

Pure mode by itself removes no duplication. The thing tasks/050
complained about - a signature stated twice with nothing comparing
them - is stated in the two pxd files, and those are exactly the two
pure mode cannot touch.

## The hybrid worth deciding on

Emit a pure-mode `.py` as the implementation file instead of a
`.pyx`, keeping the generator for all three.

This buys back the guarantee the spike README records as lost. A
`.pyx` has no Python ast, so `emitter.py`'s "impossible to emit code
that does not parse" does not apply and the spike builds lines
instead - which cost four bugs, three of which compiled fine. A
pure-mode `.py` is valid Python: `make_shared[CThing](c_name)` is a
Subscript plus a Call, `with cython.nogil:` is a With, `c_name:
string = ...` is an AnnAssign. All of it is representable in Python's
ast, so `ast.unparse` becomes available again for the largest and
most error-prone of the three emitted files.

The cost is ownership style: smart pointers rather than
`new`/`del`, because `new` cannot appear in a file Python must parse.

The two pxd files stay text-emitted either way. They are also the
small, regular ones - the current spike emits both diff-clean.

## Recommendation

Keep emitting, and put the pure-mode `.py` question to Carl as a
choice about the IMPLEMENTATION file only:

- **`.pyx` output** - full C++ vocabulary, raw pointers, matches what
  is there today; emitted as text, gated by compiling plus a golden
  diff.
- **pure `.py` output** - `ast.unparse` back, at the price of
  smart-pointer ownership everywhere and a file that parses as Python
  but does not import as it.

What pure mode is NOT is a way to stop generating. Both pxd files
remain, they remain duplicated, and they remain the reason this whole
direction started.
