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

from emit import emit

HERE = pathlib.Path(__file__).resolve().parent

# Every declaration the build compiles. Paths are relative to this
# file, so the list reads the same from any working directory.
DECLARATIONS = (
    "decl/path.py",
)


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    for name in DECLARATIONS:
        print(f"{name} -> {out}")
        emit(str(HERE / name), str(out))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: generate.py <out-dir>", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
