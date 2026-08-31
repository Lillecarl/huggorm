"""
Build integration: the codegen runs, then setuptools packages what it
wrote.

`codegen` and `huggorm-bindings` arrive via build-system (see
default.nix), so the imports below are satisfied by the standard PEP
517 build environment - no PYTHONPATH manipulation anywhere.

The generation happens HERE, at import, rather than from a `build_py`
hook. setuptools resolves and VALIDATES the package list while it
builds metadata, which is before any command runs, so a package that
does not exist yet is a package it will not build. Both spellings of
that failure are silent:

- `packages.find` answers "none", `build` decides `build_py` has
  nothing to do and skips it, the hook never fires, and the wheel
  comes out with no modules in it and no error.
- an explicit `packages = [...]` fails instead, with
  `package directory 'huggorm_generated' does not exist`.

So the package has to be on disk before `setup()` is called, and the
simplest way to say that is to write it first.
"""

import pathlib
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

from huggorm_gen.pygen.generate import main as generate
from huggorm_gen.pygen.smoke_test import main as smoke

# The stub package's directory name, owned by the emitter so setup.py
# and the generator cannot drift.
STUB_PACKAGE = "huggorm_bindings-stubs"

HERE = pathlib.Path(__file__).resolve().parent
PKG_DIR = HERE / "huggorm_generated"

generate(["--out", str(PKG_DIR)])
smoke(["--out", str(PKG_DIR)])


class build_with_stubs(build_py):
    """Only the stubs need a hook now.

    They are a PEP 561 stub-only package: a directory named
    <package>-stubs holding .pyi files and NO __init__.py. build_py
    cannot collect one - the name is not an identifier and there is no
    module to find - so it is copied wholesale into build_lib, which
    is the wheel's root.

    Everything else the generator writes is ordinary package content
    by the time setuptools looks, so `packages.find` and
    `package_data` collect it with no help.
    """

    def run(self) -> None:
        super().run()
        shutil.copytree(HERE / STUB_PACKAGE,
                        pathlib.Path(self.build_lib) / STUB_PACKAGE,
                        dirs_exist_ok=True)


# package_data lives in pyproject.toml, not here: this table would
# lose to that one, silently.
setup(cmdclass={"build_py": build_with_stubs})
