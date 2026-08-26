# Value types need __eq__, __hash__, __repr__ and __str__

Found by the Claude Fable review agent (2026-08-26 review).

## Problem

`StorePath`, `PathInfo` and `StoreLocation` define no comparison, no
hash and no repr. The effects reach users immediately:

- `PathInfo.references` returns `list[StorePath]`, and two paths
  never compare equal, so a caller compares `.to_string()` by hand.
  The repo's own tests already do this.
- A `set[StorePath]` or a dict keyed by path deduplicates nothing.
  Upstream `nix::StorePath` has `operator==` and is used as a set
  element everywhere; the binding drops that.
- `repr(sp)` prints `<cythonix_bindings.path.StorePath object at
  0x...>`, which hides the one string the object IS.

This is a DX cost that grows with every method that returns paths -
and the graph methods (044) return lists of them.

## Fix sketch

- `__eq__`/`__hash__`/`__repr__`/`__str__` on each wire-value cdef
  class, delegating to the C++ comparison where one exists
  (`nix::StorePath::operator==`) and to `_parts()` where not.
  `functools.total_ordering` is one of the two decorators Cython
  accepts on a cdef class, so ordering costs one decorator once
  `__lt__` exists.
- The codegen already skips names starting with `_`
  (extract_wrapper), so dunders never leak into the manifest, the
  protocols or the wire. Verify that with a perturbation, then rely
  on it.
- Decide the rule once and write it into the bindings README: every
  `_wire = "value"` class is comparable, hashable and printable.
  A gate in check_wire_contract can enforce it the way `_parts` is
  enforced.

## Related question: methods or properties for field reads

`info.nar_size()` and `sp.name()` are zero-arg reads spelled as
calls. cython-worker confirms the split:

- For a WRAPPED class it is a constraint, not style. The async
  surface must await, so `AsyncStore.get_uri` cannot be a
  property, and the two surfaces would disagree in spelling.
  Methods stay right for proxies.
- For the value types here it is style. PathInfo, StoreLocation
  and StorePath are `_blocking = False`, cross every layer as
  themselves, and have no async form to disagree with. Properties
  are better Python there, with no principled defence for the
  current spelling.

One real cost before switching: a cdef-class `@property` compiles
to a getset descriptor whose fget `inspect.signature` cannot read,
so `_reader_method` would type it `Any` - which the
unresolved-type gate refuses. The change therefore needs a type
source that survives compilation (the pxd backfill, or an
annotation the descriptor keeps). The manifest shape does not
change: `_reader_method` already emits `params: []`, so a property
and a zero-arg method are the same protocol dict.

Decide before the surface grows; eight fields on PathInfo become
eighty across a real store surface, and renaming call sites later
costs every consumer.
