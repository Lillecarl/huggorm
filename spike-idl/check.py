"""
The gate. What the declaration produces, against what the repo has.

Two claims, checked the same way: emit from the declaration, diff
against the artefact the real build produced by another route, and
print what does not match.

1. **The Cython.** `path.pyx`, `path.pxd` and `c_path.pxd` against the
   hand-written files in `cythonix-bindings`. Comments and docstring
   wording differ, because a human wrote those and the emitter did
   not. What may NOT differ is a single line of code, so this
   compares the two with comments and docstrings stripped.

2. **The manifest.** The `StorePath` wrapper entry against the one in
   the built `manifest.json`, which `model.py` produced by importing
   the compiled extension and reflecting on it. This one compares
   whole, field for field, because both sides are data.

The second is the interesting one. It says the manifest was knowable
from a text file before any compiler ran.

    nix run --file .. ourPython -- check.py

`--manifest <path>` points at a built manifest.json. Without it, the
manifest half is skipped and says so rather than passing quietly.
"""

import argparse
import json
import pathlib
import re
import sys

import emit
import manifest
from read import read

ROOT = pathlib.Path(__file__).resolve().parent.parent
BINDINGS = ROOT / "cythonix-bindings" / "cythonix_bindings"

# Differences that are not differences, each with the reason it is
# not one. Listed and PRINTED rather than normalised away: the same
# bargain the custom hatch makes, because an allowance nobody counts
# is where the real divergence goes to hide.
ACCEPTED = (
    ("path.pyx", "c_name", "c_base_name",
     "a local variable. The human named it after the type, the emitter "
     "derives it from the parameter."),
)


def code_only(text: str) -> list[str]:
    """A file reduced to the lines that instruct a compiler.

    Comments and docstrings carry a human's reasons, and the emitter
    writes its own. Neither is what "the same binding" means, so
    neither is compared."""
    out = []
    in_doc = False
    for raw in text.splitlines():
        line = raw.strip()
        # A docstring here is always its own statement: this repo's
        # Cython puts one at the top of a def and nowhere else, so
        # counting the fences is enough and a parser is not needed.
        if in_doc:
            if line.endswith('"""'):
                in_doc = False
            continue
        if line.startswith('"""'):
            if not (len(line) > 3 and line.endswith('"""')):
                in_doc = True
            continue
        if not line or line.startswith("#"):
            continue
        # Whitespace inside a line is not a difference either:
        # `hash_part "hashPart"()` and `hash_part "hashPart" ()` are
        # one declaration written two ways, so runs collapse and a
        # space before a paren goes.
        out.append(re.sub(r" +\(", "(", re.sub(r"\s+", " ", line)))
    return out


def check_cython(decl_path: pathlib.Path) -> list[str]:
    mod = read(str(decl_path))
    cls = mod.classes[0]
    files = emit.cython_files(cls, mod.name, mod.doc)
    problems = []
    for fname, emitted in files.items():
        actual = BINDINGS / fname
        if not actual.exists():
            problems.append(f"{fname}: nothing to compare against")
            continue
        want, got = code_only(actual.read_text()), code_only(emitted)
        for fn, theirs, ours, why in ACCEPTED:
            if fn != fname:
                continue
            print(f"  {fname}: allowing '{theirs}' -> '{ours}': {why}")
            want = [line.replace(theirs, ours) for line in want]
        if want == got:
            print(f"  {fname}: {len(got)} lines of code, identical")
            continue
        problems.append(f"{fname}: emitted code differs")
        for line in sorted(set(want) - set(got)):
            problems.append(f"    only in the repo: {line}")
        for line in sorted(set(got) - set(want)):
            problems.append(f"    only emitted:     {line}")
    return problems


def check_manifest(decl_path: pathlib.Path,
                   manifest_path: pathlib.Path) -> list[str]:
    mod = read(str(decl_path))
    cls = mod.classes[0]
    got = manifest.entry(cls, "cythonix_bindings", mod.name)
    built = json.loads(manifest_path.read_text())
    want = built["wrappers"].get(cls.name)
    if want is None:
        return [f"{cls.name} is not in {manifest_path}"]

    problems = []
    for key in sorted(set(want) | set(got)):
        if want.get(key) == got.get(key):
            continue
        problems.append(f"  {key}:")
        problems.append(f"    reflected:   {json.dumps(want.get(key))}")
        problems.append(f"    declaration: {json.dumps(got.get(key))}")
    if not problems:
        print(f"  {cls.name}: {len(want)} fields, identical to the "
              f"reflected entry")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("declaration", nargs="?", default="path.py")
    ap.add_argument("--manifest", default="")
    args = ap.parse_args()
    here = pathlib.Path(__file__).resolve().parent
    decl = here / args.declaration

    print(f"reading {decl.name} (parse only - it never runs)")
    problems = []
    print("emitted Cython vs the repo's:")
    problems += check_cython(decl)

    if args.manifest:
        print("declared manifest entry vs the reflected one:")
        problems += check_manifest(decl, pathlib.Path(args.manifest))
    else:
        print("manifest: SKIPPED - pass --manifest <manifest.json>")

    if problems:
        print("\n".join(["", "FAILED:", *problems]))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
