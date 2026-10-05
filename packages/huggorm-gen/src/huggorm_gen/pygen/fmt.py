"""Lay emitted Python out with `ruff format`.

The emitters build an AST and `ast.unparse` it, which writes valid
code with no layout: one line per statement, however long. ruff owns
the layout instead, so no emitter wraps or spaces its own text.

`--isolated`: the build sandbox holds no `ruff.toml`, and a checkout
holds the lab's. The settings are stated here so both agree.
"""

import pathlib
import subprocess

SETTINGS = ["--isolated", "--line-length", "100", "--target-version", "py314"]


def format_paths(*paths: pathlib.Path) -> None:
    """Format each file, or every Python file under each directory, in place."""
    subprocess.run(["ruff", "format", "--quiet", *SETTINGS, *map(str, paths)],
                   check=True)
