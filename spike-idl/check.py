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
    ("PathInfo", "info", "out",
     "a local variable in _from_parts. The human named it after the "
     "class, the emitter names it the same in every produced value."),
    ("StoreLocation", "loc", "out",
     "the same local, abbreviated differently - which is the argument "
     "for deriving it."),
    ("PathInfo", "def deriver(self) -> StorePath:",
     "def deriver(self) -> StorePath | None:",
     "the emitted annotation is RIGHT and the repo's is wrong. "
     "PathInfo.deriver returns None for a path added straight to the "
     "store, which its own docstring and its own wire_fields "
     "(StorePath?) both say. See tasks/052."),
)


def code_only(text: str) -> list[str]:
    """A file reduced to the STATEMENTS that instruct a compiler.

    Comments and docstrings carry a human's reasons, and the emitter
    writes its own. Neither is what "the same binding" means, so
    neither is compared.

    Statements, not lines. Where a human breaks a long call across
    four lines and the emitter writes one, that is a difference in
    typesetting rather than in code - so a line opened inside a
    bracket joins the line that opened it, and two adjacent string
    literals become the one string they already were."""
    out: list[str] = []
    in_doc = False
    depth = 0
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
        # one declaration written two ways.
        line = re.sub(r" +\(", "(", re.sub(r"\s+", " ", line))
        if depth > 0 and out:
            out[-1] = re.sub(r"\(\s+", "(", f"{out[-1]} {line}")
        else:
            out.append(line)
        depth += sum(line.count(c) for c in "([")
        depth -= sum(line.count(c) for c in ")]")
        depth = max(depth, 0)
        # Two literals a human wrapped across lines are one string.
        out[-1] = re.sub(r'"\s+"', "", out[-1])
    return out


def allow(want: list[str], target: str) -> list[str]:
    """Apply this target's allowances to the repo's side.

    An identifier is replaced on a word boundary, and anything else
    must match a whole statement. A plain substring replace is what
    the first version did, and it rewrote `query_path_info` into
    `query_path_out` - an allowance quietly editing code it was never
    meant to touch is worse than no allowance."""
    for name, theirs, ours, why in ACCEPTED:
        if name != target:
            continue
        print(f"  {target}: allowing '{theirs}' -> '{ours}': {why}")
        if theirs.isidentifier():
            want = [re.sub(rf"\b{re.escape(theirs)}\b", ours, line)
                    for line in want]
        else:
            want = [ours if line == theirs else line for line in want]
    return want


def class_body(text: str, name: str) -> str:
    """One `cdef class` out of a module that holds several.

    The repo packages PathInfo, StoreLocation and Store into one
    store.pyx. The emitter writes a class at a time, so the comparison
    is per class - which is the honest unit anyway: what is being
    checked is a binding, not a file layout."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith(f"cdef class {name}")), None)
    if start is None:
        return ""
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line[0].isspace():
            break
        out.append(line)
    return "\n".join(out)


def check_produced(decl_path: pathlib.Path, pyx_name: str) -> list[str]:
    """Each produced class, against its class in the repo's module."""
    mod = read(str(decl_path))
    actual = (BINDINGS / pyx_name).read_text()
    problems = []
    for cls in mod.classes:
        if not cls.decl.built_by:
            print(f"  {cls.name}: SKIPPED - not a produced value")
            continue
        want = code_only(class_body(actual, cls.name))
        if not want:
            problems.append(f"{cls.name}: not found in {pyx_name}")
            continue
        got = code_only("\n".join(emit.produced_pyx(cls)))
        want = allow(want, cls.name)
        if want == got:
            print(f"  {cls.name}: {len(got)} statements, identical")
            continue
        problems.append(f"{cls.name}: emitted code differs")
        for line in want:
            if line not in got:
                problems.append(f"    only in the repo: {line}")
        for line in got:
            if line not in want:
                problems.append(f"    only emitted:     {line}")
    return problems


def check_cython(decl_path: pathlib.Path) -> list[str]:
    mod = read(str(decl_path))
    if len(mod.classes) != 1:
        # store.py's classes live inside the repo's store.pyx beside
        # Store, which the emitter cannot write - nix::Store is
        # abstract and opened by a URI. So they are checked a class at
        # a time instead, by check_produced.
        return check_produced(decl_path, "store.pyx")
    cls = mod.classes[0]
    files = emit.cython_files(cls, mod.name, mod.doc)
    problems = []
    for fname, emitted in files.items():
        actual = BINDINGS / fname
        if not actual.exists():
            problems.append(f"{fname}: nothing to compare against")
            continue
        want, got = code_only(actual.read_text()), code_only(emitted)
        want = allow(want, fname)
        if want == got:
            print(f"  {fname}: {len(got)} lines of code, identical")
            continue
        problems.append(f"{fname}: emitted code differs")
        for line in sorted(set(want) - set(got)):
            problems.append(f"    only in the repo: {line}")
        for line in sorted(set(got) - set(want)):
            problems.append(f"    only emitted:     {line}")
    return problems


# Where the two routes disagree and the DECLARATION is right. Each
# one is a bug in the reflected manifest, with the evidence that says
# so - not a gap in this route, and not something to normalise away.
#
# A disagreement not listed here is a real failure.
KNOWN_WRONG = {
    ("PathInfo", "dunders"):
        "reflection cannot tell a Cython richcompare slot from an "
        "implemented comparison. PathInfo defines only __eq__, so Cython "
        "fills all six slots and `getattr(cls, '__lt__') is not "
        "object.__lt__` answers True. The shipped stub declares __lt__, "
        "so sorted(infos) typechecks - and raises TypeError at runtime.",
    ("StoreLocation", "dunders"):
        "the same cause as PathInfo's, found independently. Both refuse "
        "`a < b` at runtime with TypeError, and both are stubbed as "
        "ordering - so the bug is systemic to every value type that "
        "defines __eq__ without ordering, not a quirk of one class.",
}

# The same, one return type at a time. `methods` is not in KNOWN_WRONG
# above, because excusing the whole key would excuse every future
# change to any of nine methods. Each entry pins BOTH values, so a
# reflected entry that changes stops being excused and fails.
KNOWN_WRONG_RETURNS = {
    ("PathInfo", "ca"): (
        "typing.Union[str, None]", "str | None",
        "one type, two spellings. model.py reads a typing object and "
        "renders it; the declaration says what the source said."),
    ("PathInfo", "deriver"): (
        "StorePath", "StorePath | None",
        "the reflected entry contradicts ITSELF: its own wire_fields "
        "say StorePath?. The hand-written pyx annotates deriver without "
        "the None its docstring documents returning."),
}


def _methods_differ(name: str, want: list, got: list) -> list[str]:
    """Method lists compared one method at a time.

    Only a return type this file pins on BOTH sides is excused.
    Anything else - a renamed method, a changed parameter, a return
    type that moved - is a failure."""
    problems = []
    if [m["name"] for m in want] != [m["name"] for m in got]:
        return [f"  {name}.methods: the method NAMES differ",
                f"    reflected:   {[m['name'] for m in want]}",
                f"    declaration: {[m['name'] for m in got]}"]
    for a, b in zip(want, got, strict=True):
        if a == b:
            continue
        pinned = KNOWN_WRONG_RETURNS.get((name, a["name"]))
        if (pinned and a["return_type"] == pinned[0]
                and b["return_type"] == pinned[1]
                and a["params"] == b["params"]
                and a["doc"] == b["doc"]):
            print(f"    {a['name']}: the reflected return type is WRONG. "
                  f"{pinned[2]}")
            continue
        problems.append(f"  {name}.methods[{a['name']}]:")
        problems.append(f"    reflected:   {json.dumps(a)}")
        problems.append(f"    declaration: {json.dumps(b)}")
    return problems


def check_manifest(decl_path: pathlib.Path,
                   manifest_path: pathlib.Path) -> list[str]:
    mod = read(str(decl_path))
    built = json.loads(manifest_path.read_text())
    problems = []
    for cls in mod.classes:
        got = manifest.entry(cls, "cythonix_bindings", mod.name)
        want = built["wrappers"].get(cls.name)
        if want is None:
            problems.append(f"{cls.name} is not in {manifest_path.name}")
            continue
        differ = [k for k in sorted(set(want) | set(got))
                  if want.get(k) != got.get(k)]
        known = [k for k in differ if (cls.name, k) in KNOWN_WRONG]
        real = [k for k in differ if (cls.name, k) not in KNOWN_WRONG]
        agreed = len(set(want) | set(got)) - len(differ)
        print(f"  {cls.name}: {agreed} of {len(set(want) | set(got))} "
              f"fields agree")
        for key in known:
            print(f"    {key}: the reflected entry is WRONG. "
                  f"{KNOWN_WRONG[(cls.name, key)]}")
        if "methods" in real:
            real.remove("methods")
            problems += _methods_differ(cls.name, want["methods"],
                                        got["methods"])
        for key in real:
            problems.append(f"  {cls.name}.{key}:")
            problems.append(f"    reflected:   {json.dumps(want.get(key))}")
            problems.append(f"    declaration: {json.dumps(got.get(key))}")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("declaration", nargs="*", default=["decl/path.py", "decl/store.py"])
    ap.add_argument("--manifest", default="")
    args = ap.parse_args()
    here = pathlib.Path(__file__).resolve().parent
    names = args.declaration or ["path.py", "store.py"]
    problems = []
    for name in names:
        decl = here / name
        print(f"{decl.name} (parse only - it never runs)")
        print("  emitted Cython vs the repo's:")
        problems += check_cython(decl)
        if args.manifest:
            print("  declared manifest entry vs the reflected one:")
            problems += check_manifest(decl, pathlib.Path(args.manifest))
        else:
            print("  manifest: SKIPPED - pass --manifest <manifest.json>")

    if problems:
        print("\n".join(["", "FAILED:", *problems]))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
