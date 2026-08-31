# A declared error does not reach the caller as itself

**OPEN.** Found by the parity suite the first time it ran
(`tests/test_parity.py`, 065).

`StoreLike` is what makes an `AsyncStore` and an `RPCStore`
interchangeable. An exception type is part of a result, so this has
to work against the protocol:

    try:
        await store.parse_store_path(p)
    except BadStorePath:
        ...

It works against `huggorm_bindings.Store` and against nothing else.
On the async and rpc surfaces the caller gets
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
(tasks/036). A declared Nix error has no `to_dict`, so it is not
recognised as typed and gets wrapped.

Three surfaces, three different answers to one question, and the two
that disagree are the two the protocol says are interchangeable.

## Why the fix is not a one-liner

The obvious change - let a declared error through untouched - breaks
the server. `faults.details(wrapper)` reads `wrapper.code` and
`wrapper.message` to build the Fault detail, and a raw
`BadStorePath` has neither. So passing it through locally would
leave the remote surface unable to encode it at all, which trades a
parity gap for a worse one.

## The shape the fix probably has

Make a declared error BE a typed wrapper error, rather than teaching
the runtime to recognise one.

`errors.py` is emitted from `decl/errors.py`, so the emitter can give
each class the two things the duck-type wants: a `code` derived from
its name and a `to_dict` derived from its `_wire_fields`. Then it
passes `hasattr(e, "to_dict")` untouched on every surface, and
`faults.py` can still encode it - better than today, because it would
encode the declared class rather than an InternalError carrying it.

Two things to check before starting:

- `WrapperError.code` is a string the wire already carries. Whether a
  declared error's code should be its class name or something
  narrower is a wire decision, so it belongs in the declaration.
- 29 places in the suite name `InternalError`, `cause_type` or
  `__cause__`. Most describe a genuine internal failure - a C++ bug,
  a programming error - and should keep working. The ones that
  describe a DECLARED error are the ones that change.

## How the suite records it today

`tests/test_parity.py` asserts BOTH forms, through
`declared_error(kind, obj)`: the sync surface must raise the class,
and the other two must raise `InternalError` whose `__cause__` is
that class.

An xfail was tried first and is wrong here twice. It cannot be
applied per-surface - one of the three already behaves, so a strict
xfail XPASSes on `sync`. And it would go quiet on the day the
behaviour is fixed, where this fails and says which branch to delete.
