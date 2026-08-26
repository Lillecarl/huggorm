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

import pathlib
import sys

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


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    for name in MODULES:
        print(f"{name} -> {out}")
        emit(str(HERE / name), str(out))
    for name, fname in INCLUDES:
        mod = read(str(HERE / name))
        produced = [c.name for c in mod.classes if c.decl.built_by]
        (out / fname).write_text(produced_pxi(mod, name))
        print(f"{name} -> {out / fname}: {', '.join(produced)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: generate.py <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
