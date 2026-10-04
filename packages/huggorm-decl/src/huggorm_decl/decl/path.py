"""
The first binding of a REAL Nix type (huggorm#15).

nix::StorePath is the smallest thing that proves the whole chain:
pkg-config linkage against libnixstore, a namespaced C++ class, a
constructor that validates and throws, and accessors returning views
into the object's own storage.

It stands beside the mock's StorePath rather than replacing it. The
mock still backs everything else, and a spike that broke the working
surface would prove nothing.
"""

from huggorm_dsl.declare import (
    I64,
    Bint,
    Bytes,
    Cxx,
    Field,
    Str,
    StrView,
    binding,
    binds,
    cxx_name,
    header,
    local,
    needs,
    produced,
    startup,
    translator,
    wire_value,
)


# 100% C++, by decision: the C API is not feature complete, so it is
# not a fallback for the awkward cases either.
@header("nix/store/path.hh")
@binding(
    # nix::StorePath deletes its default constructor. That costs
    # nothing: the binding reaches it through a pointer that starts
    # NULL and a factory assigns, never by default-constructing.
    cxx="nix::StorePath",
    # libstore holds a set of these, not a vector - queryValidPaths,
    # computeFSClosure and addToStore all take StorePathSet. The wire
    # carries a list, so every `list[StorePath]` parameter converts,
    # and saying it here is what stops a dozen call sites saying it.
    collection="nix::StorePathSet",
    # pool: nothing here blocks or touches shared state.
    threading="pool",
    # Every method is a substring of a string already in memory, so
    # there is nothing to release the GIL for and no thread to hop to.
    blocking=False,
)
@wire_value(
    # The base name IS the value, so the one field is read by the one
    # accessor that renders it whole.
    fields=(Field("base_name", read="to_string"),),
    # nix::StorePath defaults operator== and operator<=>, so the
    # binding declares them rather than comparing base names in
    # Python: if upstream ever gives a store path a second field, this
    # follows without an edit.
    compare="cxx",
    order=True,
    text="to_string",
)
class StorePath:
    """A real nix::StorePath.

    Constructible, unlike the mock's StorePath, because the real class
    has a public constructor that takes a base name and validates it.
    That is the honest surface: `StorePath("<hash>-<name>")` either
    gives a store path or raises."""

    # `base_name`, because nix/store/path.hh:45 declares
    # `StorePath(std::string_view baseName)` and the rule is to follow
    # Nix unless there is a reason not to. nanopynix calls this
    # parameter `path`, which also reads badly beside the class's own
    # `name()` accessor - `name` is the part after the hash, and a
    # base name is the whole of it.
    def __init__(self, base_name: Str) -> None:
        """Raises when the name is not a store path. The message comes
        from libstore, which is the whole point of binding it."""
        ...

    # Every accessor returns a view INTO the object's own string, which
    # is what StrView says. The emitter copies before anything reaches
    # Python: a view outliving its owner is a dangling pointer, not an
    # exception.
    def to_string(self) -> StrView:
        """The full base name, '<hash>-<name>'."""
        ...

    def name(self) -> StrView:
        """The part after the hash."""
        ...

    @cxx_name("hashPart")
    def hash_part(self) -> StrView:
        """The 32-character base-32 hash."""
        ...

    @cxx_name("isDerivation")
    def is_derivation(self) -> Bint:
        """Whether the name ends in '.drv'."""
        ...


# --- what an error carries beyond its message -----------------------

# Declared here because this module holds the translator, and a record
# is visible only in the unit that declares it. Every other module
# imports this one, so its translator serves the whole process.


@produced(by="NixError.info")
@binding(threading="pool", blocking=False)
@wire_value(fields=(
    # Like a log line: the file is bytes the filesystem raised, and
    # `file` decodes strict UTF-8. The name stays, so only the type
    # flips.
    Field("file", read="file_bytes"), "line", "column",
))
class Position:
    """A place in Nix source: a file, a line and a column."""

    def file(self) -> Str:
        """The file, or Nix's name for a string or stdin.

        Strict UTF-8, so a path in any other encoding raises here
        and reads through `file_bytes`."""

    def line(self) -> I64:
        """The line."""

    def column(self) -> I64:
        """The column."""

    @local
    def file_bytes(self) -> Bytes:
        """The bytes of `file`, exactly as the filesystem raised them.

        A `Cxx` body, like the log's `text_bytes`: `nb::bytes` takes
        only explicit constructors, so a derived `@reads` body does
        not compile.
        """
        Cxx("return nb::bytes(self.file.data(), self.file.size());")

    @staticmethod
    def _from_parts() -> Position:
        """Rebuild one from the parts that crossed.

        The `file` part arrives as the bytes `file_bytes` read, so
        the aggregate needs them back in a string unread.
        """
        Cxx("""
return huggorm::Position{std::string(file.c_str(), file.size()), line, column};
        """)


@produced(by="ErrorInfo.traces")
@binding(threading="pool", blocking=False)
@wire_value(fields=(
    # Like the file's: a hint quotes paths Nix raised as bytes.
    Field("hint", read="hint_bytes"), "pos",
))
class Trace:
    """One frame of an evaluation trace, `--show-trace`'s unit."""

    def hint(self) -> Str:
        """What Nix was doing, such as "while evaluating the attribute
        'x'". It carries Nix's escape sequences.

        Strict UTF-8, so a hint quoting other bytes raises here and
        reads through `hint_bytes`."""

    def pos(self) -> Position | None:
        """Where, or None when the frame has no position."""

    @local
    def hint_bytes(self) -> Bytes:
        """The bytes of `hint`, exactly as Nix raised them."""
        Cxx("return nb::bytes(self.hint.data(), self.hint.size());")

    @staticmethod
    def _from_parts() -> Trace:
        """Rebuild one from the parts that crossed.

        The `hint` part arrives as the bytes `hint_bytes` read.
        """
        Cxx("""
return huggorm::Trace{std::string(hint.c_str(), hint.size()), pos};
        """)


@produced(by="NixError.info")
@binding(threading="pool", blocking=False)
@wire_value(fields=(
    # Like the log's: a message is bytes Nix raised, and every text
    # here decodes strict UTF-8. Every name and position stays, so
    # only the types flip.
    "level", Field("msg", read="msg_bytes"), "pos", "is_from_expr",
    "status", "traces", "truncated",
    Field("suggestions", read="suggestions_bytes"),
))
class ErrorInfo:
    """What `nix::ErrorInfo` holds, beside the rendered message.

    Capped, because an error crosses the wire in a status header:
    32 trace frames and 4096 bytes for each string."""

    def level(self) -> I64:
        """`nix::Verbosity`: 0 error, 1 warn, and upward."""

    def msg(self) -> Str:
        """The message alone, with no position and no trace. It
        carries Nix's escape sequences.

        Strict UTF-8, so a message of other bytes - `throw` carries
        whatever string it was given - raises here and reads through
        `msg_bytes`."""

    @local
    def msg_bytes(self) -> Bytes:
        """The bytes of `msg`, exactly as Nix raised them."""
        Cxx("return nb::bytes(self.msg.data(), self.msg.size());")

    def pos(self) -> Position | None:
        """Where the error is, or None when Nix gave no position."""

    def is_from_expr(self) -> Bint:
        """Whether a Nix expression raised it with `throw` or
        `abort`."""

    def status(self) -> I64:
        """The exit status Nix's CLI gives for this error."""

    def traces(self) -> list[Trace]:
        """The evaluation trace, in the order Nix prints it: the
        outermost frame first. Past 32 frames, the outermost ones go,
        because the frames nearest the error say the most."""

    def truncated(self) -> Bint:
        """Whether Nix held more frames than `traces` does."""

    def suggestions(self) -> list[Str]:
        """Nix's "did you mean" names, the best match first.

        Strict UTF-8 like the rest, so a suggestion of other bytes
        raises here and reads through `suggestions_bytes`."""

    @local
    def suggestions_bytes(self) -> list[Bytes]:
        """The suggestions as bytes, exactly as Nix raised them."""
        Cxx("""
std::vector<nb::bytes> out;
for (auto & s : self.suggestions)
    out.emplace_back(s.data(), s.size());
return out;
        """)

    @staticmethod
    def _from_parts() -> ErrorInfo:
        """Rebuild one from the parts that crossed.

        The text parts arrive as the bytes their `*_bytes` readers
        read, so each goes back into a string unread.
        """
        Cxx("""
std::vector<std::string> kept;
kept.reserve(suggestions.size());
for (auto & s : suggestions)
    kept.emplace_back(s.c_str(), s.size());
return huggorm::ErrorInfo{level, std::string(msg.c_str(), msg.size()),
    pos, is_from_expr, status, traces, truncated, std::move(kept)};
        """)


# --- what the module does before a caller exists -------------------

# Neither of these is surface. They are declared because this is
# where a module's C++ facts live, and a module that does not bind
# libstore needs neither - which is what the emitter used to assume
# and get wrong.


@needs("huggorm_decl/cpp/libstore.hpp")
@binds("huggorm::init_libstore")
@startup
def _init_libstore() -> None:
    """Initialise libstore, once, at import.

    libstore does not raise when it has not been initialised: it
    ABORTS the process, with "The program must call nix::initNix()
    before calling any libstore library functions". A binding cannot
    let a caller discover that, so this runs before anything else in
    the module - including the imports, which run another module's
    initialisation.
    """


@needs("huggorm_decl/cpp/errors.hpp", "huggorm_decl/cpp/error_info.hpp")
@binds("huggorm::translate_nix_error")
@translator
def _translate_nix_error() -> None:
    """Map a nix exception onto the right class in errors.py.

    nix has an exception hierarchy worth keeping - BadStorePath is a
    different answer from InvalidPath - and nanobind's default would
    flatten every one of them to RuntimeError.
    """
