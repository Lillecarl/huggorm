"""
Python exceptions for what real Nix throws.

GENERATED. The source is `huggorm_decl/decl/errors.py`, which is this
module plus one `cxx = "nix::..."` line per class.

The hierarchy mirrors libnixutil's own, because catching by type is the
point: `nix::BadStorePathName` derives from `BadStorePath` derives from
`Error`, so a caller who wants any bad path catches the middle one and
a caller who wants only a bad NAME catches the leaf.

That same inheritance ORDERS the translator's catch chain. C++ takes
the first catch that matches, so a base listed before its subclass
swallows it - `nix::Error` first would make every one of these a
NixError. Nothing maintains the order; it is computed from the classes
below.

Only what a binding can actually raise lives here. A class with no
`cxx` gets no catch clause, which is how a Python-only exception stays
in the module without inventing one.

Every class in this module reaches the wire. An error crosses as a
NAME checked against a declared set, which is what keeps a status
message from naming any importable class (tasks/036). The set is
this file: the emitter writes the module and knows where it put it.
"""


class NixError(Exception):
    """Anything nix::Error, and the base of everything below.

    Carries the message twice. `str(e)` is plain, because escape codes
    are wrong in a traceback and wrong over the wire. `e.colored` is
    what libstore actually wrote - the colour exists so an error can be
    printed to a terminal, and stripping it at the boundary would take
    that away from every caller who has one."""

    cxx = "nix::Error"
    header = "nix/util/error.hh"

    # What crosses the wire, in constructor order: an error is rebuilt
    # as cls(*parts) on the far side. Declared once and inherited,
    # because every error here IS a nix::Error and carries the same two
    # strings. The same word as a wire value's declaration, meaning the
    # same thing - the parts this object can be rebuilt from - though
    # an error travels in the gRPC status details rather than as a
    # response message of its own (tasks/036).
    _wire_fields = (("message", "str"), ("colored", "str"))

    def __init__(self, message: str, colored: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.colored = message if colored is None else colored

    @property
    def code(self) -> str:
        """The label this error's class travels under.

        The runtime recognises a TYPED error - one it must pass to the
        caller untouched rather than bury in an InternalError - by
        `to_dict`, and reads `code` off it. A declared Nix error had
        neither, so every one of them reached an async or rpc caller
        as somebody else's cause and `except BadStorePath` worked
        against the compiled binding alone (tasks/066).

        The class NAME, because that is already the identity the wire
        uses: an error crosses as a message type named for its class,
        and a second spelling would be a second name to keep in step.
        Computed rather than written per class, so a new error class
        needs no line here.

        A property rather than nine emitted class attributes. The
        emitter would have derived the same string from the same
        name, in a file that must then be read to learn what a caller
        can already ask the class."""
        return type(self).__name__

    def __eq__(self, other: object) -> bool:
        """Two errors are equal when they are the same class carrying
        the same parts.

        Python compares exceptions by IDENTITY, which is right for an
        exception and wrong for this hierarchy: every class here
        declares `_wire_fields`, which says it is a thing rebuilt from
        its parts on the far side of a wire. Two of them carrying the
        same parts ARE the same answer, and a round trip that produced
        an unequal object would be reporting a bug it does not have.

        It became load-bearing when a VALUE gained an error field. A
        KeyedBuildResult's `__eq__` is over its parts, and one of its
        parts is an exception - so without this, two results reporting
        the same failure compare unequal (tasks/071).

        Same CLASS, not a subclass: BadStorePathName is not a
        BadStorePath carrying the same message, and `except` is the
        place a hierarchy is meant to be read."""
        if type(other) is not type(self):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def __hash__(self) -> int:
        """As `__eq__`, over the same parts.

        Defining `__eq__` alone would set this to None and make every
        declared error unhashable, which is a thing a caller may
        reasonably want to do with one."""
        return hash((type(self).__name__,
                     tuple(sorted(self.to_dict().items()))))

    def to_dict(self) -> dict[str, str]:
        """This error as its declared parts, for a peer to rebuild.

        The duck-type the runtime looks for. It is emitted beside the
        wrappers and must not know which library it wraps, so it asks
        an error whether it can describe itself rather than testing
        it against a class (tasks/036).

        Derived from `_wire_fields` rather than naming `message` and
        `colored`: a subclass that declares more parts gets them here
        with no edit, and a method that listed the two would be the
        same fact stated twice."""
        parts = {name: str(getattr(self, name))
                 for name, _ in self._wire_fields}
        return {"code": self.code, **parts}


class UsageError(NixError):
    """nix::UsageError - the caller asked for something incoherent."""

    cxx = "nix::UsageError"
    header = "nix/util/error.hh"


class SysError(NixError):
    """nix::SystemError, including its errno-carrying nix::SysError.

    Named for the narrower C++ class rather than the wider one on
    purpose: a Python `SystemError` would shadow a builtin, and an
    `except SystemError` catching the wrong thing is exactly the kind
    of silence this hierarchy exists to remove."""

    cxx = "nix::SystemError"
    header = "nix/util/error.hh"


class Unsupported(NixError):
    """nix::Unsupported - this store cannot do that at all.

    Not a failure of the call: a statement about the store. nix::Store
    gives a default implementation for methods only some stores can
    answer, and that default throws this. A substituter has no list of
    every path it holds, and a binary cache has no directory on this
    filesystem - both are honest, and both are different from an
    operation that went wrong.

    Worth its own class rather than a message to match, because the
    right response usually differs: a caller can fall back to another
    store, and cannot fall back from a genuine error."""

    cxx = "nix::Unsupported"
    header = "nix/store/store-api.hh"


class InvalidPath(NixError):
    """nix::InvalidPath - the store has no such path.

    Different from BadStorePath, and the pair is worth keeping apart:
    BadStorePath means the STRING is not a store path, this means the
    string is a fine store path and the store does not hold it. One is
    a caller's mistake and the other is a fact about the store, so a
    caller can substitute or build after this and cannot after the
    other."""

    cxx = "nix::InvalidPath"
    header = "nix/store/store-api.hh"


class BadStorePath(NixError):
    """nix::BadStorePath - not a store path."""

    cxx = "nix::BadStorePath"
    header = "nix/store/store-dir-config.hh"


class BadStorePathName(BadStorePath):
    """nix::BadStorePathName - a store path whose name part is invalid."""

    cxx = "nix::BadStorePathName"
    header = "nix/store/store-dir-config.hh"


class BuildError(NixError):
    """nix::BuildError - a build that did not produce its outputs.

    Two things at once in C++, and that is upstream's own design:
    `nix::BuildError` inherits `nix::Error` and is throwable, AND it
    is the failure arm of `BuildResult`'s variant. So the same class
    is what `buildPaths` throws and what `buildPathsWithResults`
    RETURNS.

    No `cxx`, which means no catch clause, which means `build_paths`
    still raises a plain NixError today. That is deliberate: adding a
    catch here would change what an existing method raises, and this
    task is about the method that does not raise at all (tasks/071).
    The class exists so a RESULT can carry one.

    Carries two parts more than a NixError. `status` says which of
    the twelve failure words this is - a `BuildFailureStatus` member,
    typed as `str` because a StrEnum member IS one and this module
    cannot import the vocabulary. `is_non_deterministic` is upstream's
    own hedge: False does not mean the build IS deterministic, only
    that nothing here saw evidence otherwise.
    """

    _wire_fields = (("message", "str"), ("colored", "str"),
                    ("status", "str"), ("is_non_deterministic", "bool"))

    def __init__(self, message: str, colored: str | None = None,
                 status: str = "misc-failure",
                 is_non_deterministic: bool = False) -> None:
        super().__init__(message, colored)
        self.status = status
        self.is_non_deterministic = is_non_deterministic
