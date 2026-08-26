"""The build entry point: declarations in, Cython out.

`emit.py` is the emitter and `check.py` is the gate. This is neither.
It is the one file the BUILD runs, and it exists so the build has a
single command with a single answer to "which declarations, and where
do their files go".

    python3 generate.py <out-dir>

Nothing here decides anything. The list below is the whole
configuration, and each entry is a declaration that owns a module in
`cythonix_bindings`. A declaration not listed here emits nothing, and
a module listed here has no hand-written source at all: the three
files land in <out-dir> and Cython compiles those.

This is the step that turns the spike from a claim into the build. Up
to here the emitter wrote files beside the hand-written ones and a
gate diffed them, which proves the emitter COULD have written the
binding. Running it here proves it DID.
"""

import argparse
import ast
import pathlib

from cythonix_idl import generate_nb, manifest, pyenum
from cythonix_idl.emit import emit, produced_pxi
from cythonix_idl.read import read

HERE = pathlib.Path(__file__).resolve().parent

# Declarations that own a WHOLE module, through nanobind. Each emits
# one C++ translation unit and there is no hand-written source for it
# at all - no pyx, no pxd, no shim header.
#
# The `.pyx` route these two took is gone. Every workaround it needed
# went with it: a store path crossed as its base name and was parsed
# back, absence was the empty string, a set became a vector of
# strings, and a bound object came back as an owning raw pointer.
# nanobind casts all four, so the declaration stopped carrying them.
NANOBIND = (
    "decl/path.py",
    "decl/store.py",
)

# Declarations that own a whole module through CYTHON. Empty, and
# that is the state this spike was arguing for rather than an
# oversight: `emit.py` still works and nothing is left for it to do.
MODULES: tuple[str, ...] = ()

# Declarations the emitter has taken over only PART of, as
# (declaration, include file). Also empty now - `store.pyx` was the
# one hand-written module with declared values spliced into it, and
# there is no store.pyx.
INCLUDES: tuple[tuple[str, str], ...] = ()

# Vocabularies. A StrEnum whose members ARE the strings a Nix parser
# takes, so there is no C++ and nothing to compile - the emitted
# module is plain Python and the build writes it whole, the way it
# writes a MODULES entry.
VOCABULARIES = (
    "decl/content_address.py",
)


# Which package the emitted bindings land in. The one fact a
# declaration does not carry: where a binding is installed is the
# build's decision.
PACKAGE = "cythonix_bindings"


def nanobind_modules() -> tuple[str, ...]:
    """The module name of each declaration compiled through nanobind.

    The build loops over these to know what to emit and what to
    compile, and `setup.py` reads the same list. One place names
    them, which is the same rule the Cython route followed."""
    return tuple(pathlib.Path(name).stem for name in NANOBIND)


def declared_entries() -> dict[str, dict]:
    """Every declared class, as the manifest entry it implies.

    What `codegen` calls instead of reflecting. It builds its manifest
    by parsing the pxd files and importing the compiled extension,
    which is what puts every surface above it behind a C++ compiler.
    Calling this lets it stop, one class at a time.

    A function call, not a file. An earlier version wrote JSON and
    handed the path over, which bought nothing: the specification is
    Python and so is its reader, so a serialisation in between is one
    more shape to keep in step and one more place a field can go
    missing quietly.

    Only classes are here. Enums, errors and free functions still
    come from reflection, so this is a seam that widens rather than a
    switch that flips.

    And only classes the emitter has FINISHED. A declaration under way
    describes a class it does not yet cover - `decl/store.py` carries
    four of nix::Store's eighteen methods today - and an entry built
    from half a declaration is not a smaller answer, it is a wrong
    one. The test is structural rather than a list to keep in step: a
    MODULES declaration owns a whole module and is complete by
    construction, and an INCLUDES declaration is complete for the
    values it emits, which is what `is_value` already says."""
    out = {}
    for name in MODULES + NANOBIND:
        mod = read(str(HERE / name))
        for cls in mod.classes:
            out[cls.name] = manifest.entry(cls, PACKAGE, mod.name,
                                           final=False)
    for name, _ in INCLUDES:
        mod = read(str(HERE / name))
        for cls in mod.classes:
            if cls.is_value:
                out[cls.name] = manifest.entry(cls, PACKAGE, mod.name,
                                               final=False)
    return out


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    for name in NANOBIND:
        mod = read(str(HERE / name))
        target = out / f"{mod.name}.cpp"
        generate_nb.main(name, f"{PACKAGE}.{mod.name}", str(target))
    for name in MODULES:
        print(f"{name} -> {out}")
        emit(str(HERE / name), str(out))
    for name, fname in INCLUDES:
        mod = read(str(HERE / name))
        produced = [c.name for c in mod.classes if c.is_value]
        (out / fname).write_text(produced_pxi(mod, name))
        print(f"{name} -> {out / fname}: {', '.join(produced)}")
    for name in VOCABULARIES:
        source = HERE / name
        mod = read(str(source))
        target = out / f"{mod.name}.py"
        tree = ast.parse(source.read_text())
        target.write_text(pyenum.module(mod, tree, mod.doc) + "\n")
        words = [c.name for c in mod.classes if c.is_words]
        print(f"{name} -> {target}: {', '.join(words)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    a = ap.parse_args()
    raise SystemExit(main(a.out_dir))
