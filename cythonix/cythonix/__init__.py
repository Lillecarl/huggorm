"""
Nix, from Python.

    import cythonix

    store = cythonix.Store("auto")
    path = store.add_to_store("hello", b"hello\\n")
    for dep in store.compute_fs_closure([path]):
        print(dep)

Three packages build this library and one imports it. `cythonix` is
the front door; `cythonix_bindings` (compiled), `cythonix_generated`
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

The `Mock*` classes. They bind a C++ stand-in this repo grew before
real Nix was linked, they are on their way out, and a front door that
advertised them would be advertising scaffolding. They are still in
`cythonix_bindings` for the tests that use them.
"""

from cythonix_bindings import ContentAddressMethod as ContentAddressMethod
from cythonix_bindings import EvalState as EvalState
from cythonix_bindings import Hash as Hash
from cythonix_bindings import HashAlgorithm as HashAlgorithm
from cythonix_bindings import PathInfo as PathInfo
from cythonix_bindings import Store as Store
from cythonix_bindings import StoreLocation as StoreLocation
from cythonix_bindings import StorePath as StorePath
from cythonix_bindings import Value as Value
from cythonix_bindings import collect_garbage as collect_garbage
from cythonix_bindings import gc_release_thread as gc_release_thread
from cythonix_bindings import gc_stats as gc_stats
from cythonix_generated import AsyncEvalState as AsyncEvalState
from cythonix_generated import AsyncStore as AsyncStore
from cythonix_generated import AsyncValue as AsyncValue
from cythonix_generated import EvalStateLike as EvalStateLike
from cythonix_generated import RPCEvalState as RPCEvalState
from cythonix_generated import RPCStore as RPCStore
from cythonix_generated import RPCValue as RPCValue
from cythonix_generated import StoreLike as StoreLike
from cythonix_generated import ValueLike as ValueLike
from cythonix_generated._runtime import set_pool_size as set_pool_size

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
    "ConnectionExpired",
    "ContentAddressMethod",
    "EvalState",
    "EvalStateLike",
    "Hash",
    "HashAlgorithm",
    "NixClient",
    "PathInfo",
    "RPCEvalState",
    "RPCStore",
    "RPCValue",
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
