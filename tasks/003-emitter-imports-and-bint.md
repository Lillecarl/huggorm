# Emitter import gap and bint normalization

Review finding 3 (HIGH, proven). Emitted wrappers annotate params with
types they never import (get_type_hints NameError; masked only by
Python 3.14 lazy annotations). Cython's `bint` passes through the
model verbatim, so schema maps boolean/is_gc_managed to Handle and
every bint-returning RPC dies server-side today.

Fix: emitter collects ALL annotation names used (params + returns) and
imports them; model normalizes bint/C-only names; smoke gains a
get_type_hints sweep over every emitted module.
