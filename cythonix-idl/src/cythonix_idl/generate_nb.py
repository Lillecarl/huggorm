"""The build entry point for the OTHER backend.

`generate.py` is this file's twin: it runs `emit.py` over the
declarations and writes Cython. This runs `nbemit.py` over the same
declarations and writes C++. Neither knows about the other, and the
declaration knows about neither.

    python3 -m cythonix_idl.generate_nb <declaration> <dotted> <out-file>

One declaration at a time, because a nanobind extension is one
translation unit and the build already names the module it compiles.
That is a file, not a class: a declaration file owns a module and may
declare several classes in it, so all of them land in the one unit.

`dotted` is where the module goes. `path` on its own is the standalone
shape a spike builds; `cythonix_bindings.path` is the shape inside a
package, and it is also what lets a module import the sibling whose
types it names.
"""

import pathlib
import sys

from cythonix_idl.nbemit import bindable, extension
from cythonix_idl.read import read

HERE = pathlib.Path(__file__).resolve().parent


def main(decl: str, dotted: str, out: str) -> int:
    mod = read(str(HERE / decl) if not pathlib.Path(decl).is_absolute()
               else decl)
    bound = bindable(mod)
    if not bound:
        print(f"{decl}: nothing to bind", file=sys.stderr)
        return 2
    pathlib.Path(out).write_text(extension(mod, dotted))
    names = ", ".join(c.name for c in bound)
    print(f"{decl} -> {out} (module {dotted}): {names}")
    # What was left out, and why. A declaration under way declares
    # more than the emitter can carry, and a count that only ever
    # goes up is the honest way to see how much is left.
    skipped = [c.name for c in mod.classes if c not in bound]
    if skipped:
        print(f"  not bound: {', '.join(skipped)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("usage: generate_nb.py <declaration> <dotted> <out-file>",
              file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main(*sys.argv[1:]))
