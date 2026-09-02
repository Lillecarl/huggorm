"""
Gates over the DECLARATION reader and the emitters that read a tree.

Nothing here builds or imports a binding. What it drives is the half
of the build that turns a declaration into a document, on declarations
written for the test - so it can state a case the real corpus does not
have, and does not have to invent one there.

A version branch is that case. `read.py` imports every declaration so
Python resolves `if NIX_VERSION >= ...`, and the errors emitter parsed
the file a second time and saw neither arm (tasks/073). No declaration
in this repo branches today, so nothing real can hold the fix.
"""

import ast
import pathlib
from collections.abc import Callable

import pytest

BRANCHED = '''"""Two exception classes, one behind a version."""

from huggorm_dsl.declare import NIX_VERSION


class NixError(Exception):
    """The base every other one derives from."""

    cxx = "nix::Error"


if NIX_VERSION >= (2, 0):

    class Here(NixError):
        """The arm this build has - the version is long past."""

        cxx = "nix::Here"

else:

    class Gone(NixError):
        """The arm it does not have."""

        cxx = "nix::Gone"
'''

# The same hierarchy with nothing to resolve. Written out rather than
# derived from the one above: the two differ in exactly the thing
# under test, and computing one from the other would hide it.
FLAT = '''"""Two exception classes, neither behind a version."""


class NixError(Exception):
    """The base every other one derives from."""

    cxx = "nix::Error"


class Here(NixError):
    """Beside it, not under an `if`."""

    cxx = "nix::Here"
'''


def _declaration(tmp_path: pathlib.Path, source: str) -> str:
    """One declaration on disk, as a path its reader can open.

    A fresh directory per call. `read.load` caches an imported
    declaration by PATH, so two tests writing different text to one
    name would read the first one twice."""
    out = tmp_path / "errs.py"
    out.write_text(source)
    return str(out)


def test_a_version_branch_is_resolved_before_an_emitter_sees_it(
        tmp_path: pathlib.Path) -> None:
    """The class the import kept reaches every output; the other
    reaches none.

    Three outputs come off an exception declaration - the manifest
    entries, the translator's catch chain, and the emitted module -
    and before this they disagreed about what a branch means. The
    entries and the chain read the top level, where a branched class
    is not; the module copied the whole document, so it emitted both
    the class and the `if` around it.

    So `Gone` must be absent from all three and `Here` present in all
    three. Asserting only one direction would pass on an emitter that
    kept everything."""
    from huggorm_dsl.read import load, resolved
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    tree = resolved(path)

    entries = pyerrors.entries(tree, load(path))
    assert sorted(entries) == ["Here", "NixError"]
    # ...and it inherits, which is the half only the IMPORT knows.
    assert entries["Here"]["bases"] == ["NixError"]

    chain = "\n".join(pyerrors.chain(tree, "raise_as", "pkg.errors"))
    assert "nix::Here" in chain
    assert "nix::Gone" not in chain
    # Most-derived first, or the base swallows the subclass.
    assert chain.index("nix::Here") < chain.index("nix::Error")

    emitted = pyerrors.module(tree, "emitted")
    assert "class Here" in emitted
    assert "Gone" not in emitted


def test_the_emitted_module_keeps_no_trace_of_the_branch(
        tmp_path: pathlib.Path) -> None:
    """Three things must not survive into the emitted exception module.

    The `if` itself, because the arm is chosen at build time and a
    condition left in the output would have to be evaluated again by
    something that cannot answer it.

    The `cxx` line, which names C++ no Python caller can act on. It
    was stripped from a top-level class and not from a branched one,
    because the strip walked the top level only.

    And the import of the declaration LANGUAGE. A branch has to spell
    `NIX_VERSION` to be written at all, and once the arm is chosen
    that name is unreferenced - so carrying the import would make the
    emitted package depend on a build-time one, and would fail
    `test_no_unused_imports` besides."""
    from huggorm_dsl.read import resolved
    from huggorm_gen.cppgen import pyerrors

    emitted = pyerrors.module(resolved(_declaration(tmp_path, BRANCHED)),
                              "emitted")
    tree = ast.parse(emitted)
    assert not [n for n in ast.walk(tree) if isinstance(n, ast.If)]
    assert "cxx" not in emitted
    assert "huggorm_dsl" not in emitted
    assert not [n for n in tree.body
                if isinstance(n, ast.Import | ast.ImportFrom)]


def test_the_errors_emitter_refuses_a_tree_nothing_resolved(
        tmp_path: pathlib.Path) -> None:
    """A raw parse is the wrong tree, and saying so is the gate.

    This is what catches a caller reverting to `Corpus.tree`, and
    nothing else can: for a declaration that does not branch the two
    trees are identical, and no declaration in this repo branches.
    So the emitter refuses the shape a raw parse has and a resolved
    one cannot - a surviving `ast.If`.

    All three readings, because all three used to walk the body on
    their own and that is how they came to disagree."""
    from huggorm_dsl.read import DeclarationError, load
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, BRANCHED)
    raw = ast.parse(pathlib.Path(path).read_text())
    mod = load(path)

    readings: list[Callable[[], object]] = [
        lambda: pyerrors.entries(raw, mod),
        lambda: pyerrors.chain(raw, "raise_as", "pkg.errors"),
        lambda: pyerrors.module(raw, "emitted"),
    ]
    for reading in readings:
        with pytest.raises(DeclarationError, match="version branch"):
            reading()


def test_a_declaration_that_does_not_branch_reads_the_same_either_way(
        tmp_path: pathlib.Path) -> None:
    """Resolving costs nothing where there is nothing to resolve.

    The claim the fix rests on: `read.resolved` is a raw parse for
    every declaration in this repo, so pointing the emitter at it
    changed no output. Measured rather than assumed, because "it
    should be a no-op" is exactly the kind of claim that is wrong."""
    from huggorm_dsl.read import resolved
    from huggorm_gen.cppgen import pyerrors

    path = _declaration(tmp_path, FLAT)
    raw = ast.parse(FLAT)
    assert pyerrors.module(resolved(path), "emitted") == \
        pyerrors.module(raw, "emitted")


# A bound class with one accessor written as an ATTRIBUTE. The word is
# Python's own and the reader keeps it (`Method.prop`); no emitter
# honours it. Written here because no declaration in the corpus uses
# it, which is how the emitter path behind it went unread from the day
# it was written until it was deleted (tasks/075).
ATTRIBUTE = '''"""One bound class whose accessor is a @property."""

from huggorm_dsl.declare import Cxx, Str, binding, header


@header("nix/util/hash.hh")
@binding(cxx="nix::Hash", threading="pool", blocking=False)
class Digest:
    """A digest, for a test that never compiles one."""

    @property
    def base16(self) -> Str:
        """The digest as lowercase hex."""
        Cxx("""
return self.to_string(nix::HashFormat::Base16, /*includeAlgo=*/false);
        """)
'''


def test_the_reader_sees_an_accessor_the_import_kept_as_a_descriptor(
        tmp_path: pathlib.Path) -> None:
    """A `@property` accessor survives the read, and says it is one.

    `_live` asks each definition the import kept for its
    `co_firstlineno`, and a `property` object has no `__code__` to ask
    - so the accessor named no live line and `_resolve` dropped its
    node as if a version branch had. Silently.

    Measured before the fix, on `PathInfo.registration_time` marked
    `@property`: the accessor was gone from the emitted binding, from
    `_parts`, from `__repr__`, from `__hash__` and from
    `_wire_fields`, and the build's only complaint came from the
    hand-written `_from_parts` body that still named it -
    `pathinfo.cpp:99: 'registration_time' was not declared in this
    scope`. A class whose `_from_parts` the emitter derives would have
    lost the field with no diagnostic at all.

    Both halves are asserted. That the accessor is there, and that it
    still reads as a property - a reader that kept it by forgetting
    what it is would pass the first and fail the class below."""
    from huggorm_dsl.read import read

    path = _declaration(tmp_path, ATTRIBUTE)
    cls = read(path).classes[0]
    assert [m.name for m in cls.methods] == ["base16"]
    assert cls.methods[0].prop


def test_the_binding_refuses_an_accessor_declared_as_an_attribute(
        tmp_path: pathlib.Path) -> None:
    """`@property` is refused, because a binding alone cannot honour it.

    Three other outputs read an accessor as a CALL: the emitted
    `_parts`, `__repr__` and `__hash__` write `h.attr(name)()`, the
    stub writes `def name(self)`, and the wire reads the parts the way
    `_parts` sends them. A `def_prop_ro` here would make this file the
    only one that agreed with the declaration.

    The refusal replaced an `_accessor` function that emitted exactly
    that, and that no declaration had ever reached - which is why its
    two-row table of optional return spellings was never seen to be
    wrong (tasks/075)."""
    from huggorm_dsl.read import read
    from huggorm_gen.cppgen import nbemit

    cls = read(_declaration(tmp_path, ATTRIBUTE)).classes[0]
    with pytest.raises(TypeError, match="ATTRIBUTE"):
        nbemit.bind_function(cls, {cls.name: cls})
