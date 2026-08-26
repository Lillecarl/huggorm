"""Plain-Python twin of c_thing.pxd.

Cython resolves `cython.cimports.c_thing` to the PXD at compile time.
Python resolves it to this file at run time, because Shadow's cimport
mock does a real `import_module`. So the same source line means the
C++ declaration when compiled and a plain object when interpreted -
which is what makes a pure-mode binding importable as ordinary Python.
"""


class CThing:
    """probe::Thing, from thing.hpp."""

    def name(self) -> str: ...
