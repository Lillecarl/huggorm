"""Lay emitted Python out with ruff: import order, then `ruff format`.

The emitters build an AST and `ast.unparse` it, which writes valid
code with no layout: one line per statement, in the order the emitter
appended it. ruff owns the order and the layout instead, so an emitter
appends each import it needs and nothing else.

`--isolated`: the build sandbox holds no `ruff.toml`, and a checkout
holds the lab's. The settings are stated here so both agree; the
first-party list is the lab's, so the front door lints clean there.
"""

import pathlib
import subprocess

SETTINGS = ["--isolated", "--line-length", "100", "--target-version", "py314"]
SORT = ["--select", "I,RUF022", "--fix", "--quiet", "--config",
        'lint.isort.known-first-party = ["huggorm", "huggorm_bindings", "huggorm_generated"]']


def format_paths(*paths: pathlib.Path) -> None:
    """Sort and format each file, or every Python file under each directory."""
    files = [str(p) for p in paths]
    subprocess.run(["ruff", "check", *SETTINGS, *SORT, *files], check=True)
    subprocess.run(["ruff", "format", "--quiet", *SETTINGS, *files], check=True)
