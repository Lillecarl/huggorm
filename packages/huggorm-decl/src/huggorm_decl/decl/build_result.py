"""
nix::BuildResult and its arms: `nix/store/build-result.hh`.

What happened to ONE target of a build, as a value rather than as an
exception. `Store.build_paths` raises on the first failure and says
nothing about what succeeded; this is the other shape upstream offers,
and upstream's own comment says why it exists:

    Note that in case of a build/substitution error, this function
    won't throw an exception, but return a BuildResult containing an
    error message.

So a caller building twenty targets gets twenty results (tasks/071).

A SUM, and one arm of it is an exception class. `using Failure =
BuildError;` is one line of upstream and it is the whole reason this
was a task rather than another declaration: `nix::BuildError` inherits
`nix::Error` and is throwable, so the failure arm of a value IS the
thing the other method would have raised.

Two arms rather than one flat value, which is upstream's own shape -
`tryGetSuccess` and `tryGetFailure` both answer a pointer. A flat
value would have to publish a status word from a merged vocabulary of
sixteen, and `success` and `error` carry different things anyway.
"""

from huggorm_decl.decl.derived_path import DerivedPath
from huggorm_decl.decl.errors import BuildError
from huggorm_decl.decl.realisation import Realisation
from huggorm_decl.decl.words import BuildSuccessStatus
from huggorm_dsl.declare import (
    I64,
    U64,
    Cxx,
    binding,
    header,
    needs,
    produced,
    reads,
    spells,
    wire_value,
)


@produced(by="Store.build_paths_with_results")
@header("nix/store/build-result.hh")
@binding(
    # A nested struct, and its own type. `BuildResult::Success` is
    # what upstream calls it; `BuildResultSuccessStatus` beside it is
    # a forward declaration of the STATUS, not of this.
    cxx="nix::BuildResult::Success",
    # Two members the struct already owns.
    threading="pool",
    blocking=False,
)
@wire_value()
class BuildSuccess:
    """The arm a result holds when the target became valid.

    Produced, never constructed: it is what a build reported, and
    there is nothing a caller could correctly build one from.
    """

    def status(self) -> "BuildSuccessStatus":
        """How it came to be valid - built, substituted, or already."""
        Cxx("return huggorm::as_word(self.status);")

    @reads("builtOutputs")
    def built_outputs(self) -> "dict[str, Realisation]":
        """Each wanted output's name, and what it turned out to be.

        Empty unless the target was a derivation whose outputs are
        content-addressed: upstream fills this from the realisations,
        and an input-addressed output's path is known before the
        build so nothing has to report it."""

    def _from_parts() -> "BuildSuccess":
        """Rebuild one from the parts that crossed.

        An aggregate: upstream declares no constructor, so the braces
        initialise the two members in order. The status arrives as the
        WORD, which is what crossed, so it goes back through the same
        switch that made it."""
        Cxx("""
return nix::BuildResult::Success{
    huggorm::from_word<nix::BuildResultSuccessStatus>(status),
    built_outputs};
        """)


@produced(by="Store.build_paths_with_results")
@header("nix/store/build-result.hh")
@binding(
    cxx="nix::KeyedBuildResult",
    threading="pool",
    blocking=False,
)
@wire_value()
class KeyedBuildResult:
    """What happened to one target, and which target it was.

    Upstream keeps two types: `BuildResult` is the answer and
    `KeyedBuildResult` is the answer plus the question. Only the
    keyed one is ever produced here - `buildPathsWithResults` returns
    a vector of them - so only the keyed one is declared, and a
    Python caller never meets a result that has forgotten what it is
    about.

    Never raises. Reading a failed result is not an exception, which
    is the whole difference from `Store.build_paths` (tasks/071):
    `error` ANSWERS with the typed error rather than throwing it, and
    it is the caller who decides what to do with one.
    """

    # WIRE ORDER: what was asked, which arm answered, and the facts
    # that hold whichever arm it is.

    @reads("path")
    def path(self) -> "DerivedPath":
        """The target this is the result of.

        The same union `build_paths` takes, so a caller can match a
        result against what they asked for."""

    def success(self) -> "BuildSuccess | None":
        """The success arm, or None when the build failed.

        `error` is the other half and exactly one of the two is
        present, because upstream's `inner` is a variant of two."""
        Cxx("""
if (auto * arm = self.tryGetSuccess())
    return *arm;
return std::nullopt;
        """)

    @needs("huggorm_decl/cpp/errors.hpp")
    def error(self) -> "BuildError | None":
        """The failure arm, or None when the build succeeded.

        A `huggorm_bindings.errors.BuildError` INSTANCE - the typed
        error, with libstore's message in both its plain and coloured
        forms, the failure word, and upstream's non-determinism hedge.
        Answered, never raised.

        A caller who wants the exception behaviour writes `raise
        result.error()`, and that is the point of answering with an
        exception rather than with a record that looks like one."""
        Cxx("""
auto * arm = self.tryGetFailure();
if (arm == nullptr)
    return nb::none();
return huggorm::as_error(huggorm::errors_module, "BuildError", *arm,
    huggorm::as_word(arm->status), arm->isNonDeterministic);
        """)

    @reads("timesBuilt")
    def times_built(self) -> U64:
        """How many times the builder ran for this target.

        More than one when the build was repeated - `--repeat` or a
        check build - and 0 when nothing ran at all."""

    @reads("startTime")
    def start_time(self) -> I64:
        """When the build started, as a Unix time. 0 when none ran.

        A POINT in time rather than a span, so it stays an integer
        where the CPU times become durations."""

    @reads("stopTime")
    def stop_time(self) -> I64:
        """When the build stopped, as a Unix time. 0 when none ran."""

    @spells("BuildFailureStatus")
    def _from_parts() -> "KeyedBuildResult":
        """Rebuild one from the parts that crossed.

        Not an aggregate, and neither half of it is. `BuildResult`
        holds a variant that defaults to an EMPTY failure, so the
        arms are assigned rather than braced; `KeyedBuildResult` has
        an explicit constructor taking the result and the key, which
        upstream wrote to work around a gcc warning.

        The failure arm arrives as a Python exception, and this reads
        its declared parts back off it - the same four `_wire_fields`
        names an error crosses under (tasks/036). The COLOUR is
        dropped here and not lost: libstore formats a message when it
        builds one, so a rebuilt error carries the plain text and
        `colored` answers the same string. Nothing but a terminal can
        tell, and inventing escape codes would be worse.

        Exactly one arm, because upstream's variant holds one. A
        result that crossed with neither is a peer that built the
        message by hand, and it rebuilds as the empty failure the
        default already is.

        The PREFIX comes off before the message goes back in, and
        that is a round trip rather than a cosmetic. `nix::Error`
        writes "error: " in front of its hint when it renders
        `what()`, and `what()` is what crossed - so rebuilding from
        the crossed string and rendering it again says "error: error:
        ...", and once more for every hop. Measured by the round-trip
        gate, which is what that gate is for."""
        Cxx("""
constexpr std::string_view ERROR_PREFIX = "error: ";
nix::BuildResult result;
if (success)
    result.inner = *success;
else if (!error.is_none()) {
    auto text = nb::cast<std::string>(error.attr("message"));
    if (text.starts_with(ERROR_PREFIX))
        text.erase(0, ERROR_PREFIX.size());
    result.inner = nix::BuildError{nix::BuildError::Args{
        huggorm::from_word<nix::BuildResultFailureStatus>(
            nb::cast<std::string>(error.attr("status"))),
        nix::HintFmt(text),
        nb::cast<bool>(error.attr("is_non_deterministic"))}};
}
result.timesBuilt = times_built;
result.startTime = start_time;
result.stopTime = stop_time;
return nix::KeyedBuildResult{result, path};
        """)
