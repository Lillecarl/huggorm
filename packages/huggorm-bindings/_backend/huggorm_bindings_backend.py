"""setuptools, which needs the generator only when it has to emit.

A static `requires` cannot say that. With HUGGORM_BINDINGS_EMITTED set,
setup.py compiles a tree the generator already wrote, and imports
nothing from it.

The hooks do not call setuptools' own: those run setup.py to collect
`setup_requires`, and setup.py imports the generator before it can say
it needs it. setup.py declares no `setup_requires`.
"""

import os

from setuptools.build_meta import (
    build_editable,
    build_sdist,
    build_wheel,
    prepare_metadata_for_build_editable,
    prepare_metadata_for_build_wheel,
)

__all__ = [
    "build_editable",
    "build_sdist",
    "build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
    "prepare_metadata_for_build_editable",
    "prepare_metadata_for_build_wheel",
]

ConfigSettings = dict[str, str | list[str]] | None


def _generator() -> list[str]:
    if os.environ.get("HUGGORM_BINDINGS_EMITTED"):
        return []
    return ["huggorm-gen", "huggorm-decl"]


def get_requires_for_build_wheel(config_settings: ConfigSettings = None) -> list[str]:
    return _generator()


def get_requires_for_build_sdist(config_settings: ConfigSettings = None) -> list[str]:
    return _generator()


def get_requires_for_build_editable(config_settings: ConfigSettings = None) -> list[str]:
    return _generator()
