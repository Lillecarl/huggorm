# A declared error does not reach the caller as itself

**DONE.** Found by the parity suite the first time it ran
(`tests/test_parity.py`, 065), and fixed the same way it was found.

`StoreLike` is what makes an `AsyncStore` and an `RPCStore`
interchangeable. An exception type is part of a result, so this has
to work against the protocol:

    try:
        await store.parse_store_path(p)
    except BadStorePath:
        ...

It worked against `huggorm_bindings.Store` and against nothing else.
On the async and rpc surfaces the caller got
`huggorm_generated._runtime.InternalError`, with the real error as
`__cause__`.

## Why

`_runtime._invoke`:

    except Exception as e:
        if hasattr(e, "to_dict"):
            raise                       # typed wrapper error
        raise InternalError(f"{method} failed", cause=e) from e

The duck-type is deliberate and documented: the runtime is emitted
beside the wrappers and must not know which library it wraps
(tasks/036). A declared Nix error had no `to_dict`, so it was not
recognised as typed and got wrapped.

Three surfaces, three different answers to one question, and the two
that disagreed are the two the protocol says are interchangeable.

## What the fix is

A declared error IS a typed error. Nothing learned to recognise one.

`decl/errors.py` gains two members on `NixError`, stated once and
inherited by every class under it:

- `code`, a property returning the class name. The wire already uses
  that name as the error's identity - a declared error crosses in a
  message type named for its class - so a second spelling would be a
  second name to keep in step.
- `to_dict`, built from `_wire_fields`. A subclass that declares more
  parts gets them with no edit.

Neither is emitted per class. The declaration IS the module once the
`cxx` lines come off, so this is nine classes' worth of behaviour in
one place, and `pyerrors.py` needed no change at all.

Two consequences in the hand-written layer, both of them a RUNTIME
agreeing with the runtime below it:

- `server._wrap` caught `WrapperError` by class, which a declared Nix
  error cannot be: the bindings are imported BY the generated runtime
  and cannot import it back. It now applies the same `to_dict`
  duck-type one layer up. Catching the class made the two layers
  disagree, and the answer a caller got depended on which saw the
  error first.
- `faults.details` encoded only a wrapper's CAUSE as its declared
  parts. It now encodes the error itself when the error is declared,
  and `rebuild` reads it back as that class. `cause_type` is what
  tells the two shapes apart: a declared error travels as itself and
  has no cause; anything else travels as an InternalError whose cause
  is approximated by name.

`_cause` and the new `_from_parts` fell out of that: rebuilding a
declared error from a detail message was already written, and both
callers now share it.

## What it cost the suite

Four tests said the old shape, and they are the four that describe a
DECLARED error. The other 25 references to `InternalError`,
`cause_type` or `__cause__` describe a genuine internal failure - a
C++ bug, a ValueError from an evaluator - and are unchanged, which is
the evidence that the change is narrow.

`tests/test_parity.py` lost its `declared_error` helper entirely. It
existed to assert both shapes at once; there is one shape now, so the
three error tests are written the way a caller writes them.

`smoke_test.py`'s evaluation-error block is the clearest of the four.
It used to read the cause_type off an InternalError; it now catches
`NixError` and reads `to_dict()["code"]`, beside the ValueError case
that still wraps. Same pairing, and now it shows what a caller sees.

## Proved by breaking

Each of the three changes, reverted on its own:

- no `to_dict` on the declaration: the build fails in `smoke_test`,
  `eval_expr` wrapped again.
- `server._wrap` back to `except WrapperError`: the three rpc parity
  error tests fail and the async ones pass, which is the layer
  boundary showing itself.
- `rebuild` without `_from_parts`: the same three fail, as
  `WrapperError` rather than as the declared class.
