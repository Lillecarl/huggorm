"""
Build integration: the codegen runs, then setuptools packages what it
wrote.

`huggorm-gen` and the declarations arrive via build-system (see
default.nix), so the imports below are satisfied by the standard PEP
517 build environment - no PYTHONPATH manipulation anywhere.

`huggorm-bindings` is not among them. The generator reads the
declarations and imports nothing compiled, which the assertion below
holds it to.

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
import sys

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

# The generator did not import the compiled bindings, and this is
# where that is PROVED rather than believed.
#
# Every Python surface used to be reflected off `huggorm_bindings`,
# which put the async wrappers, the protocols, the RPC stubs and the
# type stubs behind a C++ compiler for facts a declaration states.
# Removing the imports one by one closed that (065), and nothing in
# the code says so: a single `getattr(bindings, ...)` slipped back in
# would work perfectly and quietly restore the dependency.
#
# `sys.modules`, not a missing package. The module IS importable here
# - it is propagated, because the generated wrappers import it at RUN
# time - so absence would prove nothing and could not be arranged
# without breaking the check phase. What is checked is that nothing
# reached for it.
#
# Before `smoke`, which imports it on purpose. The smoke tests COMPARE
# what was emitted against what compiled, and that is the one job an
# import is right for.
assert "huggorm_bindings" not in sys.modules, (
    "the generator imported huggorm_bindings. Every Python surface is "
    "derived from the declarations; reflecting on the compiled package "
    "puts them all behind a C++ compiler again (tasks/065).")

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
