"""
Build integration: setuptools runs the codegen as part of the wheel
build, through the same `build_py` hook a compiler would use.

`codegen` and `cythonix-bindings` arrive via build-system (see
default.nix), so the imports below are satisfied by the standard PEP
517 build environment - no PYTHONPATH manipulation anywhere.
"""

import os
import pathlib
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py

# The stub package's directory name, owned by the emitter so setup.py
# and the generator cannot drift.
STUB_PACKAGE = "cythonix_bindings-stubs"


class build_with_codegen(build_py):
    def run(self):
        from codegen.generate import main as generate
        from codegen.smoke_test import main as smoke

        cwd = os.getcwd()
        pkg_dir = os.path.join(cwd, "cythonix_generated")
        generate(["--out", pkg_dir])
        smoke(["--out", pkg_dir])
        super().run()

        # build_py snapshots package_data before our hook runs, so the
        # generated schema needs an explicit copy into build_lib.
        for name in ("grpc_schema.pb", "py.typed"):
            dst = pathlib.Path(self.build_lib) / "cythonix_generated" / name
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(pathlib.Path(pkg_dir) / name, dst)

        # The stubs for the bindings are a PEP 561 stub-only package:
        # a directory named <package>-stubs holding .pyi files and NO
        # __init__.py. build_py cannot collect one - the name is not an
        # identifier and there is no module to find - so it is copied
        # wholesale into build_lib, which is the wheel's root.
        stubs = pathlib.Path(cwd) / STUB_PACKAGE
        shutil.copytree(stubs, pathlib.Path(self.build_lib) / STUB_PACKAGE,
                        dirs_exist_ok=True)


setup(cmdclass={"build_py": build_with_codegen},
      package_data={"cythonix_generated": ["grpc_schema.pb", "py.typed"]})
