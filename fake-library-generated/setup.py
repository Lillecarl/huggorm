"""
Build integration: setuptools runs the codegen as part of the wheel
build, exactly like Cython's build_ext hook compiles pyx sources.

`codegen` and `fake-library-bindings` arrive via build-system (see
default.nix), so the imports below are satisfied by the standard PEP
517 build environment - no PYTHONPATH manipulation anywhere.
"""

import os

from setuptools import setup
from setuptools.command.build_py import build_py


class build_with_codegen(build_py):
    def run(self):
        from codegen.generate import main as generate
        from codegen.smoke_test import main as smoke

        cwd = os.getcwd()
        pkg_dir = os.path.join(cwd, "fake_library_generated")
        generate(["--spec-dir", cwd, "--out", pkg_dir])
        smoke(["--out", pkg_dir])
        super().run()


setup(cmdclass={"build_py": build_with_codegen})
