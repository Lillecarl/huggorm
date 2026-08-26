"""Build the probe. Two modules, one C++ class, no Nix involved.

    nix run --file ../.. ourPython -- setup.py build_ext --inplace
"""
from Cython.Build import cythonize
from setuptools import Extension, setup

setup(ext_modules=cythonize(
    [Extension(name, [f"{name}.py"], language="c++",
               extra_compile_args=["-std=c++20"])
     for name in ("ann", "verify")],
    language_level=3))
