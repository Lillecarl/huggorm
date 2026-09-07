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

The question it leaves is answered, 2026-09-06. **A declaration
writes `@property` OUTERMOST**, and the refusal says so now.

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

## 2026-09-03: two of the four are already right

Still **OPEN**, and its own rule still applies: nothing needs an
attribute yet. What changed is the estimate. The section above says
four emitters and a wire predicate, and two of those five were
measured and found to need nothing.

**`pyi.py` already emits `@property`.** It does not print a stub, it
moves the declaration's own tree - and its decorator filter keeps
`d.id in BUILTIN_DECORATORS`, which is where `property` lives. So the
stub agrees with the declaration today, with no edit. The claim above
was written from the emitter it replaced.

Proven by READING, not by a property reaching it. `nbemit` refuses
the class first, so no `@property` accessor has ever reached
`pyi.py` - which is the dead-path shape `tasks/075` documents, and
the reason to say how this was checked rather than that it was.
`wire.py` is the other way round: its route was traced through code
that runs on every wire value.

**`wire.py` needs no predicate.** It reads a wire value's parts
through `_parts()` and nothing else - `value_to_msg` calls
`self._sync(obj)._parts()`, and the one route that reads a part BY
NAME is `error_to_msg`, for an exception, which is Python's own object
and has no `_parts`. `_parts` is a METHOD either way. Only the C++
that builds its body changes.

So the real list is three, and one of them is new:

- `nbemit._method`, which binds `def` and would bind `def_prop_ro`;
- `nbemit.wire_fields`, whose third element is `h.attr(name)()` - one
  expression, read by `_parts`, `__repr__`, `__hash__` and
  `_round_trip`. That is the predicate, and it is ONE place, not
  four. `__eq__` needs nothing: it compares `_parts()` against
  `_parts()`, so it reads the helper rather than the fields. The one
  site OUTSIDE `wire_fields` is `@shown`, whose repr writes
  `h.attr(shown)()` for a class with no fields at all;
- `manifest._method`, which carries no `prop` key at all, so nothing
  downstream of the manifest can know. `pygen`'s wrappers read it
  from there.

The last one is the open design question, and it is not prettiness.
An async wrapper's method is `async def name(self)`, and a property
cannot be awaited. A value class does not get one - `PathInfo` is
`threading="pool"` and `blocking=False`, so `wrapped` is False and
there is no async form to disagree with - but a wrapped class would
have to refuse `@property` or answer a coroutine from an attribute.
Whichever it is has to be a refusal, not a comment.

## `__call__` is NOT this task

It was named here as "076's family of gap" by `tasks/034`, and
carried forward as "one DSL change buys both". Measurement refuted
that, and `tasks/088` is where it went.

`@property` is attribute-versus-call: the name is kept and the
emitters read it the wrong way. `__call__` was never KEPT - the
class-body loop dropped every `__`-prefixed name, in silence. One is
a reading, the other is an absence, and nothing about `__call__`
needs `@property`.

## The decorator order is answered, 2026-09-06

Still **OPEN**, and its own rule still holds: nothing needs an
attribute, so `@property` is still refused by the emitter. What is
closed is the sub-question this file said it had to answer - whether
the markers look through a descriptor, or a declaration writes
`@property` outermost.

**Outermost.** Measured both ways rather than reasoned about:

    @instant                  AttributeError, at import
    @property                 'property' object has no attribute
    def base16(self): ...     '_instant'

    @property                 reads. prop=True instant=True
    @instant
    def base16(self): ...

The reader does not care, and that is why the answer is free. `_apply`
runs the file's decorators against a THROWAWAY and skips the builtin
ones, so `@instant` reaches the probe from either position; and `prop`
is read from the TREE, not from the live object. Only Python's own
execution cares, and only about which object gets the attribute.

### Said in the refusal, which is what the file asked for

`_descriptor_hint` in `read.py`. The import failure carried Python's
own message - accurate, and not actionable: it names the descriptor
and the attribute and stops, so a reader had to work out on their own
that two lines can simply be swapped.

    hash.py: the declaration does not import, so nothing says which
    definitions exist. AttributeError: 'property' object has no
    attribute '_instant' and no __dict__ for setting new attributes
    Write @property OUTERMOST, above @instant. A marker sets
    `_instant` on what it is handed, and a property object takes no
    attribute - so the marker has to reach the function underneath
    it. That fixes the IMPORT. An emitter still refuses a @property
    accessor - see tasks/076 - so declare it as a plain method until
    that changes.

The MARKER is derived, not listed. Every marker in `declare.py`
writes `_<name>` onto what it is handed, so the shape is the fact and
a list would go stale on the next marker.

The DESCRIPTOR is named, and only one of them is - which the first
version of this got wrong. See below.

**Honest about what the swap buys**, and that half was added on
purpose. The order fixes the IMPORT and nothing else - the emitter
still refuses `@property`, and `_method` still refuses
`@staticmethod` and `@classmethod` (`tasks/082`). A hint that stopped
at the order would send a reader to a second refusal with no warning
that one was coming, which is worse than the raw `AttributeError` it
replaces.

### One gate, three assertions, three perturbations

`test_a_marker_over_a_descriptor_says_which_order_to_write`.

1. the wrong order gets the hint, naming the marker it read and
   where the path ends;
2. **the advice is TRUE** - the file written the way the refusal says
   reads, with `prop` AND `instant` both kept. This is the assertion
   that makes the message worth having rather than plausible;
3. the CONTROL - an import failure with no descriptor in it gets no
   hint.

Each fails on its own perturbation, and the three are different:

    no hint at all              1, on "@property OUTERMOST"
    hint on every failure       3, the control
    `_apply` stops skipping     2, "the marker was not lost"
    the builtin decorators

The third is worth naming. With `_apply` applying `property` to its
own probe, `@property` outermost puts the marker UNDER a property
object, `getattr` answers False, and `instant` is lost in silence -
this repo's named failure mode, in the arm the refusal now sends
readers to. The skip is what prevents it, and nothing held that until
this gate.

Both perturbations 1 and 3 print a message beginning "errs.py: the
declaration does not import", so the two failures LOOK the same in a
truncated report. Checked rather than assumed: the full traceback
names a different assert line for each. A gate whose perturbations
are indistinguishable in the output is one nobody can tell apart
later.

### `property` is the only descriptor here, and the first draft said three

The hint matched `'(property|staticmethod|classmethod)' object has no
attribute` and branched on which. Two of those three arms could never
run. Measured on 3.14.7:

    property       REFUSES - no __dict__ for setting new attributes
    staticmethod   accepts an attribute
    classmethod    accepts an attribute

So `@instant` over a `@staticmethod` IMPORTS. The declaration then
reaches `_method`'s own refusal, which already names the real problem
- a static method read as one that takes self loses its first
parameter (`tasks/082`). The hint was never going to be the thing a
reader saw.

Dead text claiming a coverage it did not have, which is `tasks/075`'s
shape, in a file written to close a `tasks/075` descendant. Found by
probing the arm rather than by reading it, after review asked what
drove it.

**NO GATE DRIVES THE TRIM.** Putting the two arms back fails nothing:

    407 passed, 11 deselected

...and it cannot, because an unreachable alternation changes no
behaviour. That is the same position `tasks/093` recorded for
`tp_clear`, and the same answer: keep the correction, and say that
nothing tests it rather than leave it looking covered.

What IS gated is the measurement behind it. The fourth assertion in
the gate asserts a marker over a `@staticmethod` reaches
`_method`'s refusal and NOT "does not import" - so the day a Python
release makes `staticmethod` refuse an attribute, that assertion
fails and this section is what a reader finds. It guards a language
behaviour rather than this repo's code, which is worth knowing about
it.

### One thing found in passing, fixed in its own commit

`DeclarationError.__init__` does `where += f":{line}"`, and `where` is
whatever went onto `_READING`. That stack is annotated `list[str]`
and nothing enforced it, so a `pathlib.Path` gave
`TypeError: unsupported operand type(s) for +=: 'PosixPath' and
'str'` - raised INSIDE the refusal, losing the message it was about
to give.

Fixed at the one WRITE to the stack rather than at every read of it,
which took removing two copies of that write first: `read` and
`resolved` each hand-rolled the same append/try/finally/pop that
`reading` already is. See the commit for the rest.
