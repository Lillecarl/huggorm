"""The Nix surface, declared. One document per Nix class.

Nothing in `decl/` is written for a particular output. A declaration
says what a Nix class IS - its methods, their types, the C++ each one
binds, the guards a tagged union needs - and an emitter decides what
that means for C++, for a stub, for an async wrapper or for the wire.

That is why the lists below live HERE and not with an emitter. Which
declarations exist, and which of them owns a compiled module, is a
fact about this set of documents. An emitter is handed the set; it
does not own it. Two backends read these same lists, and neither may
have its own idea of what is declared.

The documents are both imported and parsed. Importing them resolves
any version branch the way Python would; parsing them keeps
everything the import throws away, such as a docstring's position or
a C++ body written as a string. No body ever runs.
"""

import functools
import pathlib

from huggorm_dsl.corpus import Corpus

# Where the documents are. Every reader opens them through this or
# through `declaration()`, so none of them reconstructs the path.
DECLARATIONS = pathlib.Path(__file__).resolve().parent / "decl"

# Declarations that own a WHOLE module, through nanobind. Each emits
# one C++ translation unit and there is no hand-written source for it
# at all - no pyx, no pxd, no shim header.
#
# The `.pyx` route these took is gone. Every workaround it needed
# went with it: a store path crossed as its base name and was parsed
# back, absence was the empty string, a set became a vector of
# strings, and a bound object came back as an owning raw pointer.
# nanobind casts all four, so the declaration stopped carrying them.
# A LIST, not a mapping of module to files, and the difference will
# matter one day. One declaration owns one module because one Nix
# header owns one class: `path-info.hh` is `pathinfo.py` is
# `huggorm_bindings.pathinfo`. The mapping becomes real the first
# time one HEADER's classes want separate declaration files -
# `store-api.hh` the day StoreLocation earns its own, or a genuinely
# multi-class header. Until then it would be machinery with no
# second case to keep it honest.
NANOBIND = (
    "path.py",
    "hash.py",
    "signature.py",
    "content_address.py",
    "derived_path.py",
    "realisation.py",
    "pathinfo.py",
    "build_result.py",
    "derivation.py",
    "gc.py",
    "store_reference.py",
    "store.py",
    "eval.py",
    "terminal.py",
)

# Vocabularies. A StrEnum whose members ARE the strings a Nix parser
# takes, so there is no C++ and nothing to compile - the emitted
# module is plain Python and the build writes it whole, the way it
# writes a NANOBIND entry.
VOCABULARIES = (
    "words.py",
)

# The exception hierarchy. One declaration, two outputs: the Python
# module a caller catches, and the C++ catch chain that raises it.
ERRORS = "errors.py"

# Declarations the BUILD does not read at all. `gates/nbcheck.py`
# reads these two and nothing else does: they declare what
# `~/Code/nanopynix` binds by hand, so the gate can put the emitter's
# answer beside a person's. That corpus is one to beat rather than a
# reference to match, and the gate is skipped on a machine without
# it.
#
# Listed here even though no emitter is handed them, because the
# census below has to account for every file in the directory. A
# fourth group is the honest way to say "read by a gate": leaving
# them out would mean the census could not be exhaustive, and an
# exhaustive census is the whole point (tasks/078).
GATES = (
    "nixstore.py",
    "storefns.py",
)


def census(root: pathlib.Path,
           listed: tuple[tuple[str, tuple[str, ...]], ...]) -> None:
    """Refuse a directory and a set of lists that disagree.

    A declaration in none of the lists is SKIPPED, and a skip reads
    as an absence: `decl/gc.py` was written, imported, parsed and
    emitted nothing, and `nix build bindings-src` succeeded without
    writing `gc.cpp` (tasks/074). The lists are right and stay - what
    was missing is the check that the directory agrees with them.

    Three disagreements, and they are different mistakes: a file
    nobody listed, a list naming a file somebody deleted, and a file
    in two lists at once. Each is named separately, because "the
    lists and the directory disagree" does not say what to do.

    The glob decides what a declaration IS, and it decides it by
    suffix: `decl/README.md` is not one, and `__pycache__` is a
    directory. So the exclusions need no list of their own."""
    on_disk = {f.name for f in root.glob("*.py")}
    seen: dict[str, str] = {}
    twice = []
    for group, names in listed:
        for name in names:
            if name in seen:
                twice.append(f"{name} is in both {seen[name]} and {group}")
            seen[name] = group
    missing = sorted(on_disk - set(seen))
    gone = sorted(set(seen) - on_disk)
    bad = []
    if missing:
        bad.append(
            f"in no list, so nothing reads them: {', '.join(missing)}. "
            f"Add each to the group that describes it, or delete it.")
    if gone:
        bad.append(
            f"listed and not on disk: {', '.join(gone)}. Remove the name "
            f"or restore the file.")
    bad += sorted(twice)
    if bad:
        raise TypeError(
            f"{root} and the declaration lists disagree. " + " ".join(bad))


@functools.cache
def corpus() -> Corpus:
    """This declaration set, ready to be read.

    The one object an emitter is handed. It carries the three lists
    above and reads each document at most once, so no emitter loops
    over `NANOBIND` itself and no two emitters can disagree about
    what the set contains.

    CACHED, so every emitter in one process shares the reads. A
    `Corpus` caches within itself, which alone bought nothing: the
    six functions in `cppgen/generate.py` are called once each by
    `pygen`, so six fresh instances read the same nine declarations
    six times over.

    Cached for the life of the process, and that is safe because
    these documents are inputs to a build rather than state it
    changes. A caller that wants a fresh read builds its own
    `Corpus` - `gates/nbcheck.py` already reads declarations that
    are not in this set at all."""
    # Here rather than inside Corpus, and the reason is corpus.py's
    # own: "WHICH documents make up the set is a different fact, and
    # that one stays with the documents." A Corpus is handed a set.
    # This is where the set is decided, so this is where it has to
    # answer for the directory it came from.
    #
    # `GATES` is not passed on. It is not a build group - no emitter
    # is handed one - and it exists only so the census can account
    # for every file.
    census(DECLARATIONS, (("NANOBIND", NANOBIND),
                          ("VOCABULARIES", VOCABULARIES),
                          ("ERRORS", (ERRORS,)),
                          ("GATES", GATES)))
    return Corpus(DECLARATIONS, nanobind=NANOBIND,
                  vocabularies=VOCABULARIES, errors=ERRORS)


def declaration(name: str) -> str:
    """One declaration by name, as a path its reader can open.

    Named through here rather than found relative to a caller's own
    file, because there are several callers in several directories
    and each one guessing its way back to `decl/` is another chance
    to guess wrong."""
    return str(DECLARATIONS / (name if name.endswith(".py")
                               else f"{name}.py"))


# The C++ helpers a declaration NAMES, and the directory to compile
# against them from.
#
# They are here rather than with the bindings for one reason: it makes
# the leaf purely mechanical. `huggorm-bindings` now holds no
# hand-written source of any kind - setuptools runs an emitter and
# compiles what it wrote - and the two things a person actually
# maintains, the declarations and the helpers they name, sit together.
#
# A helper is NOT a mapping. It is infrastructure the generated code
# calls: a GC root over a foreign collector, thread registration a
# library exposes no API for, an owner whose member ORDER is the fact.
# The test is who calls it. Generated code calls a helper; a mapping
# IS the generated code. See cpp/README.md, and the census that
# counts these lines on every build.
CPP = pathlib.Path(__file__).resolve().parent / "cpp"


def include_dir() -> str:
    """The directory to put on the compiler's include path.

    The PARENT of this package, so a header is named for the package
    that owns it - `#include "huggorm_decl/cpp/eval.hpp"` - rather
    than by a bare `cpp/` that says nothing about where it came from.
    nanobind's own `include_dir()` works the same way."""
    return str(pathlib.Path(__file__).resolve().parent.parent)
