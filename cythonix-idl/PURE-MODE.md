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

## Extending it: all but one blocker comes off

Carl asked whether pure mode can be EXTENDED to cover this. It can,
and the extension is about ninety lines using Cython's own idiom.

`Cython/Shadow.py` already publishes ONE cimport twin so that a pure
file stays importable: `sys.modules['cython.cimports.libc.math']` is
the stdlib `math`. Nothing stops the same move being made wider. What
stops it working out of the box is that `CythonCImports.__getattr__`
refuses every dunder BEFORE its `import_module` fallback, so Python's
import machinery cannot even ask for `__spec__`.

`probe/cyshims.py` replaces that mock with a real `ModuleType` whose
`__path__` IS `sys.path`. A submodule search walks `__path__` as
directories, so `cython.cimports.c_store` finds the ordinary
`c_store.py` sitting beside the binding. Two smaller pieces go with
it: stand-ins for the `libcpp` declarations a C++ binding names, and
`cython.operator`, which Shadow also leaves out.

The stand-ins keep their template parameters, so
`shared_ptr[CStorePath]` reads back as `shared_ptr[CStorePath]` rather
than as `shared_ptr` - which is the difference between "it imports"
and "a generator can write the declaration back out".

Proved on one file. The SAME `ann.py`:

- cythonized with `language="c++"`, produced a working extension type;
- imported as ordinary Python, and answered
  `Thing.__annotations__['_ptr'].spelling()` with `shared_ptr[CThing]`.

That is the property the IDL spike was built to get, obtained without
an IDL: the implementation file IS the declaration, readable by
import, and it compiles.

## What the design becomes

Per module, two files written by hand and two generated:

| file | who writes it |
| --- | --- |
| `c_store.py` | hand - the C++ surface as plain Python classes |
| `store.py` | hand - the implementation, Cython pure mode |
| `c_store.pxd` | GENERATED from `c_store.py` |
| `store.pxd` | GENERATED from `store.py`, by reflection |

Three hand-written files become two, the duplication tasks/050
complained about is gone because both pxd files are derived, and there
is no emitted `.pyx` at all - so the `ast.unparse` question disappears
rather than being answered.

## What it still costs

**Smart pointers, and a factory per constructible type.** `new` is a
SyntaxError to Python, so allocation goes through a smart pointer -
an ownership decision made by syntax rather than on merit.

That has a second cost, found by probing rather than by reasoning:
`make_shared` carries libcpp's own plain `except +`
(`Cython/Includes/libcpp/memory.pxd:107`), so a CUSTOM exception
translator declared on the constructor never runs. The call Cython
emits is `std::make_shared`, not the constructor.

The fix is a factory declared in the pxd with the right
specification - `shared_ptr[CStorePath] make_store_path(string) except
+translate_nix_error` - which is exactly what `_cpp/store.hpp` already
exists to hold. So it is not new machinery, but it IS one more shim
per constructible type, and libstore's typed errors are the whole
reason this repo binds C++ rather than reimplementing it.

**`@cython.cfunc` is erased at import.** `Shadow.py:103` sets
`cclass = ccall = cfunc = _EmptyDecoratorAndManager()`, which returns
the function unchanged - so a reading generator cannot tell which
methods are cdef, and `store.pxd` must declare every one of them.
Either the bindings avoid cfuncs (with `shared_ptr` the `_get()` NULL
guard is the only one, and it is avoidable), or `declare.py` supplies
a marker of its own that also satisfies the compiler.

**The shim patches a module Cython owns.** It depends on
`CythonCImports` existing, on Shadow publishing its mock at import
time, and on being imported AFTER `cython` so it lands second. A
Cython release could break it silently. That is a real maintenance
cost and it should be a loud test, not a comment.

**`cython.cimports` cannot be a genuine package.** Pointing `__path__`
at `sys.path` means any top-level module is reachable as a cimport
twin. Harmless here, and sloppy: a narrower finder would be better if
this ever leaves a spike.

## Recommendation, revised

Pure mode is the better target, and the earlier conclusion in this
file was wrong in its emphasis: the blockers are real but only one of
them is structural.

The one that stays is that `cdef extern from` has no pure spelling. It
stops being a problem the moment the extern pxd is GENERATED from a
plain-Python twin - and that twin is the same file that makes the
binding importable. One artifact, two jobs.

So the direction to take is not "IDL emits three Cython files". It is
"two plain Python files per module, two pxd files derived from them,
and the implementation compiled in place". Smaller, and the file a
person edits is the file that runs.

The spike so far is not wasted: `emit.py` already writes both pxd
shapes diff-clean, and those are exactly the two files this design
still needs.

## Recommendation, revised again

The section above prefers pure mode because the file a person edits
is then the file that runs. Building the parse route showed that
"the file that runs" was never the goal - it was a proxy for "one
source of truth", and it is the more expensive way to get it.

Three things changed the answer:

**A declaration that does not run has no shim to maintain.** Every
blocker listed above - the patched `CythonCImports`, the erased
`cclass`/`cfunc` markers, `cython.cimports` not being a real package
- exists only to make a source file IMPORTABLE. `ast.parse` reads the
same file and needs none of them. `cyshims.py` and its canary moved
to `probe/` and stopped being build dependencies.

**Running the source buys nothing the parse does not.** Verified: the
same `ann.py` read both ways yields the same fields, the same
methods, the same annotations. Import costs a shim layer for
identical information.

**And running it costs the ordering.** A pure-mode implementation
must COMPILE before anything reflects on it, which is the constraint
the manifest already lives under. A parsed declaration hands over the
whole manifest entry before a compiler runs - 17 of 17 fields for
StorePath, identical to the reflected one. That is not a smaller
version of the same design. It is a different dependency graph.

So: emit, do not execute. Pure mode stays useful for what the emitter
WRITES - a generated pure-mode `.py` would restore `ast.unparse`, and
with it the guarantee that emitted code parses - but it is an output
format, not the source of truth.
