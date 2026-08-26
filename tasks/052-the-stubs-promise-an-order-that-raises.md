# The stubs promise an order that raises

Found by the IDL spike, by deriving the manifest a second way and
diffing the two (spike-idl/check.py).

## Problem

The shipped stubs say `PathInfo` and `StoreLocation` support `<`,
`<=`, `>` and `>=`. Neither does. A caller who sorts gets a green
typecheck and a TypeError:

    >>> sorted(store.query_path_info(p) for p in paths)
    TypeError: '<' not supported between instances of
    'cythonix_bindings.store.PathInfo' and
    'cythonix_bindings.store.PathInfo'

`cythonix_bindings-stubs/store.pyi` declares all four, because
`manifest.json` lists them in `dunders`.

## Cause

`model.py` reads the dunders by reflection:

    "dunders": sorted(d for d in VALUE_DUNDERS
                      if getattr(cls, d, None) is not getattr(object, d))

That test cannot answer the question it is asked. A cdef class
defining ANY rich comparison gets `tp_richcompare`, and CPython then
fills every one of the six comparison slots with a wrapper. So
`PathInfo.__lt__ is not object.__lt__` is True for a class whose
`__lt__` does not exist - the slot is there and refuses.

`StorePath` passes only by luck: it is `@functools.total_ordering`
with a real `__lt__`, so the right answer and the wrong measurement
agree.

The general shape: reflection measures what the COMPILER emitted,
which is not what the source said.

## Fix sketch

The declaration knows. `order=True` says a type has an order, and
the four names follow from it - which is what `spike-idl/manifest.py`
does, and why it disagrees with the reflected entry on exactly these
two classes:

    DUNDERS = (("__eq__", "value"), ("__ne__", "value"),
               ("__hash__", "value"), ("__repr__", "value"),
               ("__lt__", "order"), ("__le__", "order"),
               ("__gt__", "order"), ("__ge__", "order"),
               ("__str__", "text"))

Without the declaration route, the narrow fix is a marker the pyx
already could carry: `_ordered = True` beside `_wire`, read the way
`_wire_fields` is, with the ordering names taken from it instead of
from `getattr`. `__eq__`, `__hash__` and `__repr__` stay reflected -
those are unambiguous, because no compiler invents them.

A test belongs with it: for every value type in the manifest, if
`dunders` names `__lt__`, then `a < b` must not raise.

## Related

- 046 gave value types their dunders. This is the manifest
  mis-reporting which ones it gave.
- The same diff found `PathInfo.deriver` annotated `-> StorePath` in
  the pyx while its own `_wire_fields` say `StorePath?` and its
  docstring says it returns None. One of the two is wrong and the
  wire_fields is the one with a test behind it.
