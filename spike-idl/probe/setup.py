from setuptools import setup, Extension
from Cython.Build import cythonize
setup(ext_modules=cythonize(
    [Extension("ann", ["ann.py"], language="c++", extra_compile_args=["-std=c++20"])],
    language_level=3))
