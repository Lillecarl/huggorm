"""The build entry point for the OTHER backend.

`generate.py` is this file's twin: it runs `emit.py` over the
declarations and writes Cython. This runs `nbemit.py` over the same
declarations and writes C++. Neither knows about the other, and the
declaration knows about neither.

    python3 generate_nb.py <declaration> <module-name> <out-file>

One module at a time rather than a list, because a nanobind extension
is one translation unit and the build already names the module it is
compiling.
"""

import pathlib
import sys

from nbemit import extension
from read import read

HERE = pathlib.Path(__file__).resolve().parent


def main(decl: str, module: str, out: str) -> int:
    mod = read(str(HERE / decl) if not pathlib.Path(decl).is_absolute()
               else decl)
    if len(mod.classes) != 1:
        print(f"{decl}: one class per extension, got {len(mod.classes)}",
              file=sys.stderr)
        return 2
    pathlib.Path(out).write_text(extension(mod.classes[0], module))
    print(f"{decl} -> {out} (module {module})")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: generate_nb.py <declaration> <module> <out-file>",
              file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(*sys.argv[1:]))
