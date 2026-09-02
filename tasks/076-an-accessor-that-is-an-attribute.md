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
they are handed, and a `property` object takes none. `load` swallows
that and returns None, so the file reads tree-only and every file
importing from it does too - silently. Any work here must decide
whether the marker decorators look through a descriptor, or whether
the declaration must write `@property` outermost, and must say which
in a refusal rather than in a comment.

## One more thing the reader fix changed

`_live` now looks through `staticmethod` and `classmethod` too, for
the same reason it looks through `property`: none of the three carries
`__code__`. No declaration writes either, and the emitted C++ is
byte-identical after the fix, so nothing moved. But the behaviour DID
change and no gate holds it - a declaration that writes
`@staticmethod` today is read where before it was dropped, and the
first one to do so is what will find out what the emitters make of it.

Opened 2026-09-02, while closing `tasks/075`.
