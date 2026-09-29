"""The facts a test states that each Nix spells its own way (tasks/055).

A test builds a derivation-output key or asks a collection to look at some
paths, and 2.34 and 2.35 take different types for both. Each helper takes the
fact once and answers in this build's shape. A type checker holds `NIX_2_35`
constant for each build, so every branch is checked against its own stubs.
"""

from __future__ import annotations

import os
from typing import Any, cast

import pytest

from huggorm_bindings import DrvOutput, GCOptions, Hash, HashAlgorithm, StorePath
from huggorm_bindings.errors import NixError, SysError
from huggorm_dsl.declare import NIX_2_35

if NIX_2_35:
    from huggorm_bindings import GCSpecificPaths, GCWholeStore, UnkeyedRealisation
else:
    from huggorm_bindings import Realisation

# Two distinct nix-base32 hash parts, for keys that must differ.
_HASH_PARTS = ("0" * 32, "1" * 32)


def drv_output(seed: int, output: str) -> DrvOutput:
    """A key for `output` of the derivation `seed` names."""
    return DrvOutput(*drv_output_parts(seed, output))


if NIX_2_35:

    def drv_output_parts(seed: int, output: str) -> tuple[StorePath, str]:
        """The parts of `drv_output(seed, output)`: 2.35 keys on the
        derivation's store path."""
        return (StorePath(f"{_HASH_PARTS[seed]}-sample.drv"), output)

    def some_paths(paths: list[StorePath]) -> GCSpecificPaths:
        """Where a collection looks when it looks at `paths` only."""
        return GCSpecificPaths(paths)

    def paths_of(options: GCOptions) -> list[StorePath]:
        """The paths a collection looks at, or [] for the whole store."""
        where = options.paths_to_delete()
        return [] if isinstance(where, GCWholeStore) else where.paths()

else:

    def drv_output_parts(seed: int, output: str) -> tuple[Hash, str]:
        """The parts of `drv_output(seed, output)`: 2.34 keys on the
        derivation's hash modulo."""
        return (Hash(HashAlgorithm.SHA256, bytes([seed]) * 32), output)

    def some_paths(paths: list[StorePath]) -> list[StorePath]:
        """Where a collection looks when it looks at `paths` only."""
        return paths

    def paths_of(options: GCOptions) -> list[StorePath]:
        """The paths a collection looks at."""
        return options.paths_to_delete()


if NIX_2_35:

    def built_output(path: StorePath) -> UnkeyedRealisation:
        """What a build says one output became: 2.35 drops the key."""
        return cast("UnkeyedRealisation", _from_parts(UnkeyedRealisation, path, []))

else:

    def built_output(path: StorePath) -> Realisation:
        """What a build says one output became: 2.34 keeps the key."""
        return cast("Realisation", _from_parts(Realisation, drv_output(0, "out"), path, []))


def _from_parts(kind: Any, *parts: object) -> object:
    """`_from_parts`, which the stubs leave out because it is not
    surface."""
    return kind._from_parts(*parts)


# How a cold evaluation says the file it was asked for is gone: 2.35
# checks the path before it opens it, and raises a plain Nix error.
if NIX_2_35:
    MISSING_FILE = "does not exist"
    MissingFileError: type[NixError] = NixError
else:
    MISSING_FILE = "opening file"
    MissingFileError = SysError


# Whether this build's libexpr has the collector, as the BUILD says:
# `boehm_gc()` is the binding under test and cannot vouch for itself.
HAS_COLLECTOR = os.environ.get("HUGGORM_NIX_GC", "1") == "1"

#: For a test of the collector itself; a build without one refuses its
#: questions by name, and `test_settings` holds that.
needs_collector = pytest.mark.skipif(
    not HAS_COLLECTOR, reason="this Nix was built without the collector")
