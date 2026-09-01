"""
nanobind bindings over Nix, none of them hand-written.

There will be a lot of these, and most of them exist only so that
something else can be bound. An intermediate type is not scaffolding
to be thrown together: it is the surface everything above it sees, and
the place a mistake in it surfaces is three layers away.

## Where things come from

Nothing in here is the binding. Every module's C++ is written into the
build's copy of this directory from a declaration in
`huggorm-decl/src/huggorm_decl/decl/`, and `setup.py` compiles one
nanobind extension per declaration.

One Nix header, one declaration, named after it. `nix/store/path.hh`
is `decl/path.py` and compiles to `path`; `nix/store/store-api.hh` is
`decl/store.py` and compiles to `store`. Nix's own layout is the map,
so nobody has to learn a second one.

## What IS hand-written

This module's docstring, and nothing else. Every other file here is
written by `setup.py` before setuptools is told it exists.

The C++ this repo writes is not here either. It is
`huggorm_decl/cpp/`, with the declarations that NAME it - `@binds`
points at a function in there. See its README for the rule, and for
the sharper rule about what does not belong.

`errors.py` is emitted too, from `decl/errors.py`. It is pure Python
on purpose: a compiled module would need a reason, and a class
statement is not one.

There are no markers left. `_errors_module` and `_async_twins` stood
here and both are gone: the emitter that writes `errors.py` decides
where it goes, and a word's async spelling sits beside its C++ one in
`declare.py`. Nothing in this file is read by the generator now.

## The mock

There isn't one. `fake-library/` was a C++ stand-in this repo grew
before real Nix was linked; every module here binds libstore or
libexpr now, and the stand-in is deleted (tasks/060).
"""

from .build_result import BuildSuccess, KeyedBuildResult
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
from .words import (
    BuildFailureStatus,
    BuildMode,
    BuildSuccessStatus,
    ContentAddressMethod,
    HashAlgorithm,
)

__all__ = [
    "BuildFailureStatus",
    "BuildMode",
    "BuildSuccess",
    "BuildSuccessStatus",
    "ContentAddress",
    "ContentAddressMethod",
    "DerivedPathBuilt",
    "DrvOutput",
    "EvalState",
    "Hash",
    "HashAlgorithm",
    "KeyedBuildResult",
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
