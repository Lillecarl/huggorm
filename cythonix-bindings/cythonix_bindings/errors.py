"""
Python exceptions for what real Nix throws.

The hierarchy mirrors libnixutil's own, because catching by type is the
point: `nix::BadStorePathName` derives from `BadStorePath` derives from
`Error`, so a caller who wants any bad path catches the middle one and
a caller who wants only a bad NAME catches the leaf.

Only what a binding can actually raise lives here. Adding a Nix type
means adding whatever it throws, in nix_error.hpp beside the catch that
produces it (tasks/015).

Every class in this module reaches the wire. The codegen reflects the
module named by `_errors_module` in the package __init__, so an error
crosses as a NAME checked against a declared set - which is what keeps
a status message from naming any importable class (tasks/036).
"""


class NixError(Exception):
    """Anything nix::Error, and the base of everything below.

    Carries the message twice. `str(e)` is plain, because escape codes
    are wrong in a traceback and wrong over the wire. `e.colored` is
    what libstore actually wrote - the colour exists so an error can be
    printed to a terminal, and stripping it at the boundary would take
    that away from every caller who has one."""

    # What crosses the wire, in constructor order: an error is rebuilt
    # as cls(*parts) on the far side. Declared once and inherited,
    # because every error here IS a nix::Error and carries the same two
    # strings. The same word as a wire value's declaration, meaning the
    # same thing - the parts this object can be rebuilt from - though
    # an error travels as JSON in the gRPC status rather than as a
    # message of its own (tasks/036).
    _wire_fields = (("message", "str"), ("colored", "str"))

    def __init__(self, message: str, colored: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.colored = message if colored is None else colored


class UsageError(NixError):
    """nix::UsageError - the caller asked for something incoherent."""


class SysError(NixError):
    """nix::SystemError, including its errno-carrying nix::SysError.

    Named for the narrower C++ class rather than the wider one on
    purpose: a Python `SystemError` would shadow a builtin, and an
    `except SystemError` catching the wrong thing is exactly the kind
    of silence this hierarchy exists to remove."""


class BadStorePath(NixError):
    """nix::BadStorePath - not a store path."""


class BadStorePathName(BadStorePath):
    """nix::BadStorePathName - a store path whose name part is invalid."""
