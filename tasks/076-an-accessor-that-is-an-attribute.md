# An accessor that is an attribute

**OPEN.** `@property` in a declaration is refused. This says what
honouring it would cost, so the refusal can be lifted on purpose
rather than by whoever needs it first.

## What the word means

`Method.prop` is set by Python's own `@property`, and it says an
accessor is an ATTRIBUTE rather than a call. The DSL has carried the
word since the reader was written, with this reason:

> which one an accessor is was never a property of its class -
> nanopynix presents `StorePath.to_string()` as a method and
> `ValidPathInfo.path` as an attribute, and both are values.

That is still a fair reading of the two shapes. `info.nar_size` is a
fact the object holds; `hash.to_string()` is work it does.

## What refuses it, and why

`nbemit` refuses a bound class whose accessor sets `prop`
(`tasks/075`). Not because the binding cannot do it - `def_prop_ro`
is one line - but because three other outputs read an accessor as a
CALL and would then disagree with it:

1. **`_identity_semantics`** writes `h.attr("nar_size")()` into
   `__repr__`, `__hash__` and `_parts`. One call per part, and a
   property makes every one of them call a string.
2. **`pyi.py`** writes `def nar_size(self) -> int` in the stub. A
   caller's typechecker would promise a method over an attribute.
3. **`wire.py`** and the generated async and RPC wrappers read a
   part the way `_parts` sends it.

So four emitters, one fact. That is exactly the shape goal 3 exists
for, and it is why the fix is to teach them together or not at all.

## What it would take

- `nbemit`: `def_prop_ro` with `_returns` for the type, and
  `_identity_semantics` reading `h.attr(name)` without the `()`.
- `pyi.py`: `@property` on the emitted stub method.
- The wire: one predicate - "is this part a call?" - read from the
  same `Method.prop`, used everywhere a part is fetched.
- A gate: a produced value with one attribute part and one method
  part, round-tripped. Both shapes in one class, or the predicate is
  never exercised against its opposite.

The predicate is the whole design. A boolean asked in four places
beats four places each deciding, and it is what makes the answer one
fact rather than four.

## What is NOT a reason to do it

Prettiness. `info.nar_size` reads better than `info.nar_size()` and
that is worth something, but not four emitters and a wire predicate
on its own. Do this when a declaration NEEDS an attribute - an
upstream shape a caller would misread as work, or a compatibility
surface that was one - and not before.

## What must not be forgotten

`@property` under another decorator kills the import:

    AttributeError: 'property' object has no attribute '_instant'

`@instant`, `@local`, `@reads` and the rest set an attribute on what
they are handed, and a `property` object takes none.

That used to be SILENT: `load` swallowed it, so the file read
tree-only and so did every file importing from it. `tasks/082` closed
that half - the import error now reaches a reader with Python's own
reason attached, so a declaration written this way fails and says
which line to fix.

The question it leaves is still open, and it is the one this task
has to answer: do the marker decorators look through a descriptor,
or must a declaration write `@property` outermost? Whichever it is
has to be said in a refusal rather than in a comment.

## One more thing the reader fix changed

`_live` now looks through `staticmethod` and `classmethod` too, for
the same reason it looks through `property`: none of the three carries
`__code__`. Before that they named no live line and were dropped
whole, in silence.

Both are refused in `_method` now (`tasks/082`), and the measurement
is why: with the refusal removed, `@staticmethod def of(text: Str)`
read as `of()` with no parameters at all, because `_method` reads a
bound method by skipping the first one. Teaching them is the same
four-emitter problem as `@property`, and belongs to this task if
anybody ever wants one.

Opened 2026-09-02, while closing `tasks/075`.
