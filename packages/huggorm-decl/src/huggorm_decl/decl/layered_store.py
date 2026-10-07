"""
huggorm::LayeredStore: `huggorm_decl/cpp/layered_store.hpp`.

A Nix store implemented in Python, over another store (huggorm#149).

    class Mine(LayeredStore):
        def __init__(self, config):
            super().__init__(config, Store("dummy://"))

        def is_valid_path(self, path):
            ...

    register_store_implementation("mine", ["mine"], Mine)
    Store("mine://?flavour=sweet")

A subclass overrides `Store`'s own methods, with their contracts:
`query_path_info` raises `InvalidPath` for a path the store does not
hold. Nix calls the override when it does that operation, and so does
a Python caller of the method. What the subclass does not override,
and what an override passes on with `super()`, runs the same operation
on the underlying store; with none, Nix raises `Unsupported`, as a
cache store does. A declared Nix error an override raises reaches Nix
as that error.

The hooks are the UNCACHED operations: Nix's path-info cache sits above
them, and `path-info-cache-size=0` in the URI turns it off. Nix may call
one from any thread.

In process only. A subclass is Python in this process, and the factory
a registration keeps is a Python callable.
"""

from huggorm_decl.decl.path import StorePath
from huggorm_decl.decl.pathinfo import PathInfo
from huggorm_decl.decl.store import Store
from huggorm_dsl.declare import (
    Bint,
    PyFunc,
    Str,
    binding,
    header,
    in_process,
    needs,
    reads,
    virtual,
)


@in_process
@header("huggorm_decl/cpp/layered_store.hpp")
@binding(cxx="huggorm::LayeredStoreConfig", holder="shared_ptr",
         threading="pool", blocking=False)
class LayeredStoreConfig:
    """What a store URI said: Nix opens a layered store with one, and
    hands it to the factory."""

    @reads("scheme")
    def scheme(self) -> Str:
        """The URI's scheme."""

    @reads("authority")
    def authority(self) -> Str:
        """What follows `scheme://`, before any `?`."""

    @reads("params")
    def params(self) -> dict[str, Str]:
        """The URI parameters no Nix store setting takes: the Python
        store's own."""


@in_process
@header("huggorm_decl/cpp/layered_store.hpp")
@binding(cxx="huggorm::LayeredStore", holder="shared_ptr",
         threading="pool", blocking=True)
class LayeredStore(Store):
    """The base of a store implemented in Python. See the module.

    It IS a `Store`: Nix hands the opened store back as the object the
    factory made, so `Store("mine://")` answers the subclass."""

    def __init__(self, config: LayeredStoreConfig,
                 underlying: Store | None = None) -> None:
        """A layer over `underlying`, or over nothing."""

    @virtual
    def is_valid_path(self, path: StorePath) -> Bint:
        """Whether the store holds `path`."""

    @virtual
    def query_path_info(self, path: StorePath) -> PathInfo:
        """What the store knows about `path`. Raises InvalidPath when it
        does not hold it."""

    @virtual
    def query_path_from_hash_part(self, hash_part: Str) -> StorePath | None:
        """The path whose hash part this is, or None."""

    @virtual
    def query_all_valid_paths(self) -> list[StorePath]:
        """Every path the store holds."""

    @virtual
    def query_referrers(
        self,
        path: StorePath,
    ) -> list[StorePath]:
        """The paths that refer to `path`."""

    @virtual
    def add_temp_root(self, path: StorePath) -> None:
        """Keep `path` from garbage collection while this process lives."""


@needs("huggorm_decl/cpp/layered_store.hpp")
def register_store_implementation(name: Str, schemes: list[Str],
                                  factory: PyFunc) -> None:
    """Make `schemes` open a store that `factory` makes, for the rest of
    the process.

    `Store("scheme://authority?params")` calls `factory(config)` with a
    `LayeredStoreConfig`, and the factory answers a `LayeredStore`. A
    subclass is such a factory. A name registers once; a second
    registration raises."""
    Cxx("huggorm::register_layered_store(name, schemes, factory);")
