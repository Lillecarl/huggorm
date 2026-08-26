"""
The free bindings that open a store.

nix::Store is abstract and chosen by a URI, so there is no
constructor to bind - `nixstore.py` says `@produced(by="open_store")`
and this is what it points at.

The C++ these name is hand-written, and should be. `open_store_uri`
keeps one LocalStore per state directory, because `nix::openStore`
caches nothing and two LocalStores in one process deadlock on the
temp-roots flock they each take. That is forty lines of real logic
with a mutex and a weak pointer in it, and a generator that wrote it
from a template would be guessing.

What the declaration owns is the BINDING: the Python name, the
parameter name a caller passes by keyword, and whether the call can
wait.

Opening a store CAN wait, and `@blocks` says so. A remote store is
opened over the network, so holding the GIL across the call would
stall every other Python thread for as long as that takes. A local
one waits on the temp-roots flock, and `lockFile` calls
checkInterrupt only after flock returns - so that wait, holding the
GIL, would be uninterruptible. Either alone would settle it.
"""

from typing import overload

from declare import Str, binds, blocks


@blocks
@binds("open_store_uri")
@overload
def open_store(uri: Str) -> "Store":
    """Open the store at this URI.

    `dummy://` is in-memory and needs nothing on disk, which is what
    makes it testable in a build sandbox."""


@blocks
@binds("open_store_default")
@overload
def open_store() -> "Store":
    """Open whatever the ambient configuration says, which usually
    means the daemon."""
