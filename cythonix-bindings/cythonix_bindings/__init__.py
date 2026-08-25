"""
Cython bindings over Nix.

There will be a lot of these, and most of them exist only so that
something else can be bound. An intermediate type is not scaffolding
to be thrown together: it is the surface everything above it sees, and
the place a mistake in it surfaces is three layers away.

## Where things go

One Nix header, one binding module, named after it. `nix/store/path.hh`
is `path.pyx`; `nix/store/store-api.hh` is `store.pyx`. Nix's own
layout is the map, so nobody has to learn a second one.

Each module has up to three files:

    c_<name>.pxd    what C++ declares. Nothing of ours.
    <name>.pxd      what OUR cdef classes declare, so a sibling module
                    can reach their fields. Only when one needs to.
    <name>.pyx      the binding.

The middle one is what makes dependent bindings work: `store.pyx`
validates and prints store paths, so it needs `StorePath._ptr`, and
Cython will not share a cdef class's fields across modules without a
pxd to declare them in.

`_cpp/` holds C++ this repo writes, one header per binding plus a
shared `errors.hpp`. It is for what a pxd cannot SAY - see its README
for the rule, and for the sharper rule about what does not belong.

`errors.py` is the exception hierarchy, mirroring libnixutil's own. It
is pure Python on purpose: a compiled module would need a reason, and
a class statement is not one.

## The mock

`mock_store.py`/`eval.pyx` and their pxds bind fake-library, a C++
stand-in this repo grew before real Nix was linked. It is on its way
out. Each mock class carries a Mock prefix from the moment its real
counterpart lands and takes the plain name, so the prefix is a map of
what is left to do; when it is gone, so is the mock.
"""

from .eval import EvalState, Value, collect_garbage, gc_release_thread, gc_stats
from .mock_store import (
    MockDerivation,
    MockDerivedPath,
    MockLocalStore,
    MockRemoteStore,
    MockStore,
    MockStorePath,
    describe,
)
from .path import StorePath
from .store import Store

__all__ = [
    "EvalState",
    "MockDerivation",
    "MockDerivedPath",
    "MockLocalStore",
    "MockRemoteStore",
    "MockStore",
    "MockStorePath",
    "Store",
    "StorePath",
    "Value",
    "collect_garbage",
    "describe",
    "gc_release_thread",
    "gc_stats",
]
