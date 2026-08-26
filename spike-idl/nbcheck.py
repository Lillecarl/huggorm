"""
The nanobind gate: emitted bindings against nanopynix's hand-written.

`check.py` does this for the Cython backend, diffing against
`cythonix-bindings`. This does it for the C++ one, and the reference
is better: nanopynix's bindings are hand-written, tested, and in use.

## Per accessor, not per line

A `.def` chain has no meaningful order - `.def("a", ...).def("b",
...)` binds the same class either way - so this compares a DICT of
accessor name to body. That is the honest unit: "does the emitter
produce the same binding for `nar_hash`", not "does it produce it in
the same position".

## Three kinds of difference, and only one is a failure

**COSMETIC** - a lambda parameter named `i` where the emitter derives
`vpi`. Invisible to Python. Pinned by name and printed.

**BETTER** - the emitter is right and the hand-written file is wrong.
`nb::is_operator()`, ordering, and binding a view by method pointer.
Each names the evidence; each was verified, not assumed.

**DIVERGENT** - the two projects disagree about the public API and
only a person can settle it. `path` vs `base_name` is one.

Anything else fails.
"""

import pathlib
import re
import sys

import nanobind
from read import read

NANOPYNIX = pathlib.Path.home() / "Code/nanopynix/nanopynix-bindings/src"

# A lambda parameter or local, named differently. Invisible to Python.
COSMETIC = {
    ("ValidPathInfo", "i"): "vpi",
    ("ValidPathInfo", "refs"): "refs",
    ("ValidPathInfo", "out"): "sigs",
}

# Where the emitter is RIGHT and the hand-written file is not. Each
# carries the evidence, because "mine is better" is a claim.
BETTER = {
    ("StorePath", "__eq__"):
        "nb::is_operator(). Without it a failed overload raises TypeError; "
        "with it nanobind returns NotImplemented (nb_func.cpp:530). Verified "
        "against the built module: `sp == None` raises there today.",
    ("StorePath", "to_string"):
        "bound by method pointer. <nanobind/stl/string_view.h> copies into a "
        "Python str, which is the copy the hand-written lambda was making, so "
        "the lambda hid the method behind a closure for nothing.",
    ("StorePath", "name"): "same as to_string.",
    ("StorePath", "hash_part"): "same as to_string.",
    ("StorePath", "__lt__"):
        "ordering. nix::StorePath defaults operator<=> upstream, so "
        "sorted(paths) should work. Nothing in nanopynix binds it.",
    ("StorePath", "__le__"): "ordering, as __lt__.",
    ("StorePath", "__gt__"): "ordering, as __lt__.",
    ("StorePath", "__ge__"): "ordering, as __lt__.",
}

# The two projects disagree, and only a person can settle it.
DIVERGENT = {
    ("StorePath", "__init__"):
        "nanopynix names the constructor parameter `path`, cythonix names it "
        "`base_name`, and the docstrings differ. Same C++ constructor, two "
        "public APIs. The declaration has to pick one.",
}


def strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def accessors(block: str) -> dict[str, str]:
    """Every `.def*("name", ...)` in a class chain, by name."""
    out: dict[str, str] = {}
    for m in re.finditer(r'\.(def|def_ro|def_prop_ro|def_static)\("([^"]+)"',
                         block):
        start = m.start()
        depth, i = 0, block.index("(", m.start(1))
        while i < len(block):
            if block[i] in "([{":
                depth += 1
            elif block[i] in ")]}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        body = " ".join(block[start:i + 1].split())
        out[m.group(2)] = body
    # nb::init is the constructor, and it carries no name of its own.
    if (c := re.search(r"\.def\(nb::init<.*?\)\)", block, re.S)):
        out["__init__"] = " ".join(c.group(0).split())
    return out


def hand_written(cls_name: str) -> dict[str, str]:
    for src in sorted(NANOPYNIX.glob("*.cpp")):
        text = strip_comments(src.read_text())
        m = re.search(r'nb::class_<[^>]+>\(m,\s*"' + cls_name + r'"\)', text)
        if not m:
            continue
        end = text.index("});", m.end()) + 3
        return accessors(text[m.start():end])
    return {}


def check(decl_path: str) -> list[str]:
    mod = read(decl_path)
    problems = []
    for cls in mod.classes:
        want = hand_written(cls.name)
        if not want:
            print(f"  {cls.name}: SKIPPED - not in nanopynix")
            continue
        got = accessors(strip_comments(nanobind.bind_function(cls)))
        c = nanobind.census(cls)
        print(f"  {cls.name}: {len(got)} emitted, {len(want)} hand-written "
              f"({c['derived']} derived, {c['hatched']} hatched)")

        for name in sorted(set(want) | set(got)):
            a, b = want.get(name), got.get(name)
            for (kls, old), new in COSMETIC.items():
                if kls == cls.name and a:
                    a = re.sub(rf"\b{re.escape(old)}\b", new, a)
            if a == b:
                continue
            if (cls.name, name) in BETTER:
                print(f"    {name}: EMITTER IS RIGHT. "
                      f"{BETTER[(cls.name, name)]}")
            elif (cls.name, name) in DIVERGENT:
                print(f"    {name}: THE TWO PROJECTS DISAGREE. "
                      f"{DIVERGENT[(cls.name, name)]}")
            elif a is None:
                problems.append(f"  {cls.name}.{name}: emitted, not in "
                                f"nanopynix")
            elif b is None:
                problems.append(f"  {cls.name}.{name}: in nanopynix, not "
                                f"emitted")
            else:
                problems.append(f"  {cls.name}.{name}:")
                problems.append(f"    hand-written: {a[:150]}")
                problems.append(f"    emitted:      {b[:150]}")
    return problems


def main() -> int:
    names = sys.argv[1:] or ["path.py", "pathinfo.py"]
    print(f"emitted nanobind vs {NANOPYNIX}")
    problems = []
    for n in names:
        problems += check(n)
    if problems:
        print("\n".join(["", "FAILED:", *problems]))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
