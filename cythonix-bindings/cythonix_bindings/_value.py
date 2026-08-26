"""
What every `_wire = "value"` binding does the same way.

A value type compares, hashes and prints as the thing it IS. Before
this, two StorePaths naming the same store object were never equal, a
`set[StorePath]` deduplicated nothing, and `repr()` showed an address
instead of the one string the object carries - so every caller
compared `.to_string()` by hand, this repo's own tests included
(tasks/046).

All three answers come from the SAME two declarations the wire uses:
`_wire_fields` names the parts, `_parts()` produces them. So a class
that gains a field gains it here too, with no edit, and a class whose
dunders disagreed with its wire form would be a contradiction rather
than a bug to notice.

Pure Python, like errors.py and _declare.py. A compiled module needs a
reason and this is not one: these run once per comparison of objects
whose fields are already Python.

They are functions rather than a shared base class because a cdef
class may only inherit from another extension type, and giving every
value type a common base would put it into the generated hierarchy
(018) for the sake of three dunders.
"""

from typing import Any


def _parts_of(obj: Any) -> tuple[Any, ...]:
    parts: tuple[Any, ...] = obj._parts()
    return parts


def eq(self: Any, other: Any) -> Any:
    """Equal when the same class carries the same declared parts.

    `type(other) is not type(self)` rather than isinstance: a subclass
    of a value type would carry parts this one does not compare, so
    saying "equal" would be a claim the parts do not support."""
    if type(other) is not type(self):
        return NotImplemented
    return _parts_of(self) == _parts_of(other)


def hash_(self: Any) -> int:
    """Consistent with eq(), which is the only rule a hash must keep.

    A list part becomes a tuple: `PathInfo.references` is a list, and
    a list is unhashable for the good reason that it can change - but
    a value's parts never do, so flattening it is safe here and
    nowhere else."""
    return hash(tuple(
        tuple(p) if isinstance(p, list) else p for p in _parts_of(self)))


def repr_(self: Any) -> str:
    """`Cls(field=value, ...)`, from the declared field names.

    Evaluable for a value a caller can construct, and merely honest
    for one that is produced - PathInfo has no constructor to call, so
    its repr describes rather than reconstructs."""
    fields = [name for name, _ in self._wire_fields]
    return "{}({})".format(
        type(self).__name__,
        ", ".join(f"{n}={v!r}"
                  for n, v in zip(fields, _parts_of(self), strict=True)))
