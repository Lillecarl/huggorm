"""
The exception hierarchy, re-exported at the front door.

`cythonix_bindings.errors` is where these are declared - the bindings
own them, because libstore is what raises them and the class names
cross the wire against that declared set (tasks/036). This module
exists so a caller writes `cythonix.errors.InvalidPath` and never has
to learn which of the three build packages holds it.

Re-exported by name rather than star-imported, so a typechecker sees
the same set a reader does, and so adding one upstream is a visible
edit here rather than a silent widening.
"""

from cythonix_bindings.errors import BadStorePath as BadStorePath
from cythonix_bindings.errors import BadStorePathName as BadStorePathName
from cythonix_bindings.errors import InvalidPath as InvalidPath
from cythonix_bindings.errors import NixError as NixError
from cythonix_bindings.errors import SysError as SysError
from cythonix_bindings.errors import Unsupported as Unsupported
from cythonix_bindings.errors import UsageError as UsageError

__all__ = [
    "BadStorePath",
    "BadStorePathName",
    "InvalidPath",
    "NixError",
    "SysError",
    "Unsupported",
    "UsageError",
]
