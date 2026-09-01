"""
Nix, from Python.

    import huggorm

    store = huggorm.Store("auto")
    path = store.add_to_store("hello", b"hello\\n")
    for dep in store.compute_fs_closure([path]):
        print(dep)

Three packages build this library and one imports it. `huggorm` is
the front door; `huggorm_bindings` (compiled), `huggorm_generated`
(emitted at build time) and this one are how it is made, which is not
something a caller should have to learn before the first import.

## The three surfaces

The same store, spelled three ways, and the choice is about WHERE the
work happens rather than what it does.

- **Sync** - `Store`, `StorePath`, `PathInfo`. The bindings
  themselves. Every call blocks the calling thread.
- **Async** - `AsyncStore` and friends, from `connect_local`. The same
  calls, awaited, with the blocking part moved onto a thread so an
  event loop keeps running.
- **Remote** - `connect()` gives a client whose objects satisfy the
  same protocols (`StoreLike`) and run on another process's store.

A protocol is what both async surfaces promise, so code written
against `StoreLike` runs either way.

## What is NOT here

Nothing, now. The `Mock*` classes were the last exception and they are
gone (tasks/060). Every name the two packages behind this one export
reaches this front door, and a test says so rather than a reader
having to check.
"""

from huggorm_bindings import BuildMode as BuildMode
from huggorm_bindings import ContentAddress as ContentAddress
from huggorm_bindings import ContentAddressMethod as ContentAddressMethod
from huggorm_bindings import DerivedPathBuilt as DerivedPathBuilt
from huggorm_bindings import DrvOutput as DrvOutput
from huggorm_bindings import EvalState as EvalState
from huggorm_bindings import Hash as Hash
from huggorm_bindings import HashAlgorithm as HashAlgorithm
from huggorm_bindings import MissingPaths as MissingPaths
from huggorm_bindings import OutputsSpec as OutputsSpec
from huggorm_bindings import PathInfo as PathInfo
from huggorm_bindings import Realisation as Realisation
from huggorm_bindings import Signature as Signature
from huggorm_bindings import SingleDerivedPathBuilt as SingleDerivedPathBuilt
from huggorm_bindings import Store as Store
from huggorm_bindings import StoreLocation as StoreLocation
from huggorm_bindings import StorePath as StorePath
from huggorm_bindings import Value as Value
from huggorm_bindings import collect_garbage as collect_garbage
from huggorm_bindings import gc_release_thread as gc_release_thread
from huggorm_bindings import gc_stats as gc_stats
from huggorm_generated import AsyncEvalState as AsyncEvalState
from huggorm_generated import AsyncStore as AsyncStore
from huggorm_generated import AsyncValue as AsyncValue
from huggorm_generated import EvalStateLike as EvalStateLike
from huggorm_generated import RPCEvalState as RPCEvalState
from huggorm_generated import RPCStore as RPCStore
from huggorm_generated import RPCValue as RPCValue
from huggorm_generated import StoreLike as StoreLike
from huggorm_generated import ValueLike as ValueLike
from huggorm_generated._runtime import set_pool_size as set_pool_size

# The SUM types, from the generated package rather than from the
# bindings - a union has no home there, because the alias is Python
# and the module binding its arms is a compiled extension. Re-exported
# because a caller annotating their OWN function with DerivedPath is
# the whole point of the alias having a name.
from huggorm_generated._unions import DerivedPath as DerivedPath
from huggorm_generated._unions import SingleDerivedPath as SingleDerivedPath

from . import errors as errors
from .remote import ConnectionExpired as ConnectionExpired
from .remote import NixClient as NixClient
from .remote import connect as connect
from .server import serve as serve

# Sorted flat rather than grouped: ruff keeps __all__ sorted, and
# the grouping that matters - sync, async, remote, protocol - is
# in the docstring above, where a reader looks first.
__all__ = [
    "AsyncEvalState",
    "AsyncStore",
    "AsyncValue",
    "BuildMode",
    "ConnectionExpired",
    "ContentAddress",
    "ContentAddressMethod",
    "DerivedPath",
    "DerivedPathBuilt",
    "DrvOutput",
    "EvalState",
    "EvalStateLike",
    "Hash",
    "HashAlgorithm",
    "MissingPaths",
    "NixClient",
    "OutputsSpec",
    "PathInfo",
    "RPCEvalState",
    "RPCStore",
    "RPCValue",
    "Realisation",
    "Signature",
    "SingleDerivedPath",
    "SingleDerivedPathBuilt",
    "Store",
    "StoreLike",
    "StoreLocation",
    "StorePath",
    "Value",
    "ValueLike",
    "collect_garbage",
    "connect",
    "errors",
    "gc_release_thread",
    "gc_stats",
    "serve",
    "set_pool_size",
]
