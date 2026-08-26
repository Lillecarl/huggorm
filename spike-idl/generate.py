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
import json
import pathlib

import manifest
from emit import emit, produced_pxi
from read import read

HERE = pathlib.Path(__file__).resolve().parent

# Declarations that own a WHOLE module. Each emits three files and
# there is no hand-written source for it at all.
MODULES = (
    "decl/path.py",
)

# Declarations the emitter has taken over only PART of, as
# (declaration, include file). Each emits one `.pxi` holding its
# produced values, and a hand-written `.pyx` splices it with
# `include`. See emit.produced_pxi for why an include and not a
# splice.
INCLUDES = (
    ("decl/store.py", "store_produced.pxi"),
)


# Which package the emitted bindings land in. The one fact a
# declaration does not carry: where a binding is installed is the
# build's decision.
PACKAGE = "cythonix_bindings"


def declared_entries() -> dict[str, dict]:
    """Every declared class, as the manifest entry it implies.

    The seam between this directory and the generator next door, and
    it is DATA rather than an import. `codegen` builds its manifest by
    parsing the pxd files and reflecting on the compiled extension,
    which is what puts every surface above it behind a C++ compiler.
    Handing it these entries lets it stop, one class at a time,
    without either side importing the other's modules.

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
    for name in MODULES:
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


def main(out_dir: str, manifest_out: str = "") -> int:
    out = pathlib.Path(out_dir).resolve()
    for name in MODULES:
        print(f"{name} -> {out}")
        emit(str(HERE / name), str(out))
    for name, fname in INCLUDES:
        mod = read(str(HERE / name))
        produced = [c.name for c in mod.classes if c.is_value]
        (out / fname).write_text(produced_pxi(mod, name))
        print(f"{name} -> {out / fname}: {', '.join(produced)}")
    if manifest_out:
        entries = declared_entries()
        pathlib.Path(manifest_out).write_text(
            json.dumps(entries, indent=2, sort_keys=True) + "\n")
        print(f"declared entries -> {manifest_out}: "
              f"{', '.join(sorted(entries))}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    ap.add_argument("--manifest-out", default="",
                    help="also write the declared manifest entries here")
    a = ap.parse_args()
    raise SystemExit(main(a.out_dir, a.manifest_out))
