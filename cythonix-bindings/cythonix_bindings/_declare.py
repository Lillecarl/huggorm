"""
The declarations a module-level binding function carries.

A cdef class declares itself in its own body - `_threading = "pool"`,
`_binds = "CStore"` - and that is not a style choice. Cython refuses
any decorator on a cdef class but `functools.total_ordering` and
`dataclasses.dataclass`:

    Cdef functions/classes cannot take arbitrary decorators.

Setting the attribute afterwards fails too, because an extension type
is immutable:

    TypeError: cannot set '_threading' attribute of immutable type

A module-level `def` has neither limit. It took its markers as
assignments at the bottom of the file, forty lines from the function
they described - so a reader had to hold two places in their head, and
a function with NO marker (gc_release_thread, deliberately) said so
only by being absent from a list.

These put the declaration on the function. The mechanism does not
change: each sets the same attribute the codegen already reads.

They return the function ITSELF, never a wrapper. The generator reads
`inspect.signature`, `__annotations__` and `__doc__` off what the
module exports, so a wrapper would hide the whole signature behind
`(*args, **kwargs)` and every parameter would resolve to Any.
"""

from collections.abc import Callable
from typing import Any, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

# The only policy a free function may declare. It has no instance, so
# there is no thread for it to be affine to. Named here as well as in
# the generator because this is where the mistake gets made, and an
# import-time error beats a build-time one.
POOL = "pool"


def threading(policy: str) -> Callable[[F], F]:
    """Opt one function into the generated surface, under `policy`.

    The marker is what opts a function IN. An undecorated function is
    still part of the module and still described by the stubs; it just
    gets no async form and no rpc, which is what declaring nothing
    means."""
    if policy != POOL:
        raise ValueError(
            f"a free function may only declare {POOL!r} threading (got "
            f"{policy!r}). It has no instance, so there is no thread for "
            f"it to be affine to.")

    def apply(fn: F) -> F:
        fn._threading = policy  # type: ignore[attr-defined]
        return fn

    return apply


def binds(c_name: str) -> Callable[[F], F]:
    """Name the pxd declaration this function wraps.

    Only needed when the two names differ. The generator falls back to
    the Python name, so a function called what the pxd calls it needs
    no declaration - and a name that matches nothing in the pxd fails
    the build rather than going unnoticed."""

    def apply(fn: F) -> F:
        fn._binds = c_name  # type: ignore[attr-defined]
        return fn

    return apply
