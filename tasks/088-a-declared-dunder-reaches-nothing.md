# A declared dunder reaches nothing

**OPEN.** The refusal is DONE and the support is not. A declaration
that writes `def __call__` now gets a diagnostic naming the line.
Before, it got nothing at all.

## The silent skip

`read.py`'s class-body loop read a method like this:

```python
if item.name == "__init__":
    ctor = _method(item, vocab)
    ...
elif not item.name.startswith("__"):
    methods.append(_method(item, vocab))
```

Every other `__`-prefixed name fell off the end. No binding, no stub
line, no manifest entry, no wrapper, and no diagnostic - and a skip
is indistinguishable from an absence, which is this repo's named
failure mode. The sixth found, after `tasks/073`, `075`, `078`,
`082` and `087`.

Measured on a probe declaring `__call__` and `__len__` beside one
plain accessor:

    Probe ['nar_size'] ctor= None

Two declared methods gone, and the read reported success.

No declaration in `decl/` tripped it. `errors.py` declares `__eq__`
and `__hash__`, and they are safe: an error class carries no
`@binding`, so `read()` returns no bound class for that file at all -
measured, `classes: []`. Its source is lifted verbatim by
`pyerrors.py` instead.

## Done: the refusal

The branch is inverted, so a `__`-prefixed name is REFUSED rather
than skipped:

    errs.py:11:5: Callable.__call__: a name starting with `__`
    reaches no emitter, and until now was dropped in silence.
    `__init__` is the one exception. The value dunders are derived
    from the class decorators - @wire_value writes them, and its
    `order=` and `text=` decide which - so do not declare one.
    Anything else needs the emitters taught (tasks/088); declare it
    under a plain name until then.

Through `_survive`, like every other refusal in that loop, so two
mistakes in one class are two mistakes.

The rule is `__`-PREFIXED, not "dunder". A name-mangled `__private`
lands here too and the answer is the same: Python mangles it, nothing
reads it, and the reader says so.

The message names the two answers a caller has, and neither is "wait
for this task". The value dunders are DERIVED - `@wire_value` says
which ones a class owes, through its `order=` and `text=`, and
the emitter writes them - so declaring `__repr__` restates a fact
the decorator already carries. Anything else gets a plain name, and
`Value.apply` is what `tasks/034` shipped for exactly this reason.

`@order` and `@text` are NOT decorators, which the first draft of
this message said. They are keyword parameters of `@wire_value`
(`declare.py:626`). Caught by reading the signature after the
message was written, and worth recording: the gate pins `tasks/088`
and the line, so a wrong spelling here would have passed the suite
and misled the first caller who hit it.

`tasks/082`'s idiom, and its rationale: a refusal that names where
support is tracked costs one line and turns a silent skip into a
question somebody can answer.

Broken on purpose, with the refusal replaced by the `continue` the
old code effectively did:

    FAILED test_a_declared_dunder_is_refused_rather_than_dropped
    Failed: DID NOT RAISE DeclarationError
    1 failed, 360 passed, 10 deselected

## Open: what `__call__` would take

`tasks/034` shipped `await f.apply(x)` and named `await f(x)` as its
residue. This is where that lives.

Four surfaces have to agree, and this is NOT `tasks/076`'s
problem. That was the carried assumption - "one DSL change buys
both" - and it is wrong. `@property` is attribute-versus-call in
four emitters. `__call__` is a NAME the reader never kept. Different
gaps, different fixes, and nothing about `__call__` needs
`@property`.

- **`nbemit`**: nothing. nanobind binds `.def("__call__", ...)` like
  any other name, so the C++ side needs no new word.
- **`manifest.entry`**: a SECOND silent-drop layer, and the one to
  watch. It filters `if not m.name.startswith("_")`, with a comment
  saying a leading underscore is Python's own word for "not surface".
  Teaching the reader to keep dunders makes the manifest drop them
  next, so that filter has to decide about a dunder DELIBERATELY.
- **the wire**: a proto identifier must start with a letter, so
  `__call__` cannot be an rpc method name. Either the declaration
  marks it `@local` - and what `@local` actually does through the
  manifest to `pygen` is unmeasured, so measure before betting on it
  - or the wrappers get Python-side sugar aliasing `apply`.
- **`pyi.py`**: nothing. It moves the declaration's own node.

`Value.__call__` is also in `tasks/067`'s class - nanobind resolves a
call slot at run time, so a wrong binding emits and compiles and
fails a gate. Compile and gate; do not stop at diffing the text.

A remote gate is warranted if `__call__` reaches the RPC client in
any form.

## The other thing that loop drops, and does not say

An `async def` in a class body is dropped too, and by the line above
the refusal: `if not isinstance(item, ast.FunctionDef): continue`.
`ast.AsyncFunctionDef` is not a subclass of `ast.FunctionDef`, so it
falls out with the class-body markers and the docstring.

It is NOT silent, which is why it is a note rather than a second
defect. `tasks/082`'s `_reconcile` catches it - the import keeps the
function, so it names a live line, and the tree reader has no node
there:

    probe.py: the import kept definitions at lines [16] and the tree
    has no node there. The two readings have stopped lining up -
    most likely `co_firstlineno` no longer points where this assumes.

Loud, and blaming the wrong thing. Nothing about that message says
"async", and the cause it names - a Python release moving
`co_firstlineno` - would send a reader to the wrong file.

The fix is two lines and a gate: an explicit refusal for
`ast.AsyncFunctionDef` in the class-body loop and in the
module-level function loop, and `ast.AsyncFunctionDef` added to
`_reconcile`'s node set so reconcile stops claiming it. Not done
here, because the refusal above is the silent one and this is a
diagnostic.

## What this task is NOT

An argument that `__call__` should exist. `await f.apply(x)` works
today and reads honestly. The refusal above is the part that had to
happen; the sugar is worth doing when a caller asks for it, or when
`Value` grows a second dunder that would want the same machinery.

Opened 2026-09-03, while reading `tasks/076`.
