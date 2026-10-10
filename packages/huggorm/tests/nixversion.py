"""The facts a test states that each Nix spells its own way (huggorm#55).

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

# Nix's own wording, where no type or field tells the error apart
# (huggorm#145). One place, so a Nix that rewords one branches it here,
# as MISSING_FILE does. huggorm's own messages stay in the tests.
BAD_STORE_URI = "Cannot parse Nix store"
BUILD_LOG_STORAGE = "Build log storage and retrieval"
CANNOT_ADD = "cannot add"
ERROR_PREFIX = "error: "
EVALUATING_FILE = "evaluating file"
EXPERIMENTAL_FEATURE = "experimental Nix feature"
FLAKES_DISABLED = "'flakes' is disabled"
IFD_SETTING = "allow-import-from-derivation"
INGESTION_METHODS = "expect `flat`, `nar`, or `git`"
INTERRUPTED = "interrupted"
MISSING_ARGUMENT = "argument without a value"
NO_STORE_SCHEME = "don't know how to open Nix store with scheme"
NO_SUBSTITUTER = "no substituter that can build it"
NOT_A_FUNCTION = "attempt to call something which is not a function but"
NOT_ABSOLUTE = "not an absolute path"
NOT_IN_STORE = "is not in the Nix store"
NOT_SUPPORTED_BY_SCHEME = "not supported by scheme"
NOT_SUPPORTED_BY_STORE = "not supported by store"
PATH_DOES_NOT_EXIST = "does not exist"
PURE_EVAL = "in pure evaluation mode"
SHORT_STORE_PATH = "too short to be a valid store path"
STACK_OVERFLOW = "stack overflow; max-call-depth exceeded"
STILL_ALIVE = "since it is still alive"
STRING_ORIGIN = "«string»"
SUGGESTION = "Did you mean"
UNDEFINED_VARIABLE = "undefined variable"
UNKNOWN_HASH = "unknown hash algorithm"
WORKER_OP = "performing daemon worker op"


# Whether this build's libexpr has the collector, as the BUILD says:
# `boehm_gc()` is the binding under test and cannot vouch for itself.
HAS_COLLECTOR = os.environ.get("HUGGORM_NIX_GC", "1") == "1"

#: For a test of the collector itself; a build without one refuses its
#: questions by name, and `test_settings` holds that.
needs_collector = pytest.mark.skipif(
    not HAS_COLLECTOR, reason="this Nix was built without the collector")
