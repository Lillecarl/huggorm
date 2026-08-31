"""
nanobind bindings over Nix, none of them hand-written.

There will be a lot of these, and most of them exist only so that
something else can be bound. An intermediate type is not scaffolding
to be thrown together: it is the surface everything above it sees, and
the place a mistake in it surfaces is three layers away.

## Where things come from

Nothing in here is the binding. Every module's C++ is written into the
build's copy of this directory from a declaration in
`huggorm-idl/src/huggorm_idl/decl/`, and `setup.py` compiles one
nanobind extension per declaration.

One Nix header, one declaration, named after it. `nix/store/path.hh`
is `decl/path.py` and compiles to `path`; `nix/store/store-api.hh` is
`decl/store.py` and compiles to `store`. Nix's own layout is the map,
so nobody has to learn a second one.

## What IS hand-written

`_cpp/` holds C++ this repo writes, plus a shared `errors.hpp`. It is
what a declaration CALLS rather than what a binding needs: `@binds`
points at a function in there. See its README for the rule, and for
the sharper rule about what does not belong.

`errors.py` is the exception hierarchy, mirroring libnixutil's own. It
is pure Python on purpose: a compiled module would need a reason, and
a class statement is not one.

The two markers below - `_errors_module` and `_async_twins` - are the
package's own declarations, read by the generator.

## The mock

There isn't one. `fake-library/` was a C++ stand-in this repo grew
before real Nix was linked; every module here binds libstore or
libexpr now, and the stand-in is deleted (tasks/060).
"""

from .content_address import ContentAddress
from .derived_path import (
    DerivedPathBuilt,
    OutputsSpec,
    SingleDerivedPathBuilt,
)
from .eval import EvalState, Value, collect_garbage, gc_release_thread, gc_stats
from .hash import Hash
from .path import StorePath
from .pathinfo import PathInfo
from .realisation import DrvOutput, Realisation
from .signature import Signature
from .store import MissingPaths, Store, StoreLocation
from .words import ContentAddressMethod, HashAlgorithm

# Where the exception hierarchy lives. A declaration, like _binds or
# _wire, and for the same reason: the codegen must not know a module
# name this package could rename. An error crosses the wire as a name,
# and a name is only safe to construct against a declared set - so the
# set has to come from here (tasks/036).
_errors_module = "huggorm_bindings.errors"

# A type whose ASYNC surface is spelled differently. Same value, and a
# wrapper that gives it awaitable methods: anyio.Path wraps a
# pathlib.Path so a caller who is already in an event loop can read
# the file without blocking it.
#
# Declared here rather than known by the codegen, like every other
# marker. A binding returns the sync type and says nothing about
# threads; this is the one place that says the async wrapper hands
# back the other spelling, and the generator does the rest.
#
# It never reaches the wire. A type with a twin has no protobuf field
# either way, so this decides one annotation and one constructor call
# in the in-process wrapper and nothing else.
_async_twins = {"pathlib.Path": "anyio.Path"}

__all__ = [
    "ContentAddress",
    "ContentAddressMethod",
    "DerivedPathBuilt",
    "DrvOutput",
    "EvalState",
    "Hash",
    "HashAlgorithm",
    "MissingPaths",
    "OutputsSpec",
    "PathInfo",
    "Realisation",
    "Signature",
    "SingleDerivedPathBuilt",
    "Store",
    "StoreLocation",
    "StorePath",
    "Value",
    "collect_garbage",
    "gc_release_thread",
    "gc_stats",
]
