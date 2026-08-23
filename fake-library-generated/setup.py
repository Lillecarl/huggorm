"""
Build integration: setuptools runs the codegen as part of the wheel
build, exactly like Cython's build_ext hook compiles pyx sources.

`codegen` and `fake-library-bindings` arrive via build-system (see
default.nix), so the imports below are satisfied by the standard PEP
517 build environment - no PYTHONPATH manipulation anywhere.
"""

import os
import pathlib
import shutil

from setuptools import setup
from setuptools.command.build_py import build_py


class build_with_codegen(build_py):
    def run(self):
        from codegen.generate import main as generate
        from codegen.smoke_test import main as smoke

        cwd = os.getcwd()
        pkg_dir = os.path.join(cwd, "fake_library_generated")
        argv = ["--out", pkg_dir]
        pxds = os.environ.get("PXD_FILE", "").split()
        if pxds:
            argv += ["--pxd"] + pxds
        generate(argv)
        smoke(["--out", pkg_dir])
        super().run()

        # build_py snapshots package_data before our hook runs, so the
        # generated schema needs an explicit copy into build_lib.
        src = pathlib.Path(pkg_dir) / "grpc_schema.pb"
        dst = pathlib.Path(self.build_lib) / "fake_library_generated" / "grpc_schema.pb"
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(src, dst)


setup(cmdclass={"build_py": build_with_codegen},
      package_data={"fake_library_generated": ["grpc_schema.pb"]})
