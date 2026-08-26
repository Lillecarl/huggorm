"""
The stretch target: one method that BLOCKS.

path.pyx has no `with nogil:` anywhere - every StorePath accessor is a
substring of memory the object already owns. A store talks to a daemon
or a database, so its calls wait, and the emitter has to release the
GIL around them or the whole process stops while one call is out.

This declares the smallest such method. It is NOT compiled: nix::Store
is abstract and opened by a URI, so a real Store declaration needs a
factory constructor and a shared_ptr, neither of which this spike
emits. What it proves is the nogil shape, and what it FINDS is the
first thing the emitter refuses.
"""

from declare import Bint, binding, cxx_name, header


@header("nix/store/store-api.hh")
@binding(cxx="nix::Store", threading="pool", blocking=True)
class Store:
    """A real nix::Store, opened from a URI."""

    @cxx_name("isValidPath")
    def is_valid_path(self, path: Bint) -> Bint:
        """Whether this store holds that path.

        `path` is declared Bint rather than StorePath on purpose: a
        parameter of a BOUND type is the first shape this emitter
        refuses, and the refusal is the finding. See the README."""
        ...
