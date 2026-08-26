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

import cythonix_idl
from cythonix_idl import nbemit
from cythonix_idl.read import read

NANOPYNIX = pathlib.Path.home() / "Code/nanopynix/nanopynix-bindings/src"

# A lambda parameter or local, named differently. Invisible to Python.
COSMETIC = {
    ("ValidPathInfo", "i"): "vpi",
    ("ValidPathInfo", "refs"): "refs",
    ("ValidPathInfo", "out"): "sigs",
}

# Where the emitter is RIGHT and the hand-written file is not.
#
# Each entry PINS what makes it better, as text the emitted line must
# contain (or must not). An unpinned "mine is better" excuses every
# future difference on that accessor, the emitter REGRESSING to match
# the hand-written version included - verified: with the reason alone
# and no pin, deleting nb::is_operator() from the emitter left this
# gate green.
#
# The reason is for a reader; the pin is what the gate checks, and it
# is checked UNCONDITIONALLY rather than as an explanation for a diff.
# Verified why: deleting nb::is_operator() makes the emitted line
# identical to the hand-written one, so there is no diff left to
# explain and a pin checked only on difference never fires. The claim
# is an invariant about the emitter, not a note about a disagreement.
BETTER = {
    ("StorePath", "__eq__"): (
        "nb::is_operator()",
        "Without it a failed overload raises TypeError; with it nanobind "
        "returns NotImplemented (nb_func.cpp:530). Verified against the "
        "built module: `sp == None` raises there today."),
    ("StorePath", "__lt__"): (
        "nb::is_operator()",
        "ordering. nix::StorePath defaults operator<=> upstream, so "
        "sorted(paths) should work. Nothing in nanopynix binds it."),
    ("StorePath", "__le__"): ("nb::is_operator()", "ordering, as __lt__."),
    # Found by parity.py, which is a stronger instrument than this
    # file: it imports the two COMPILED modules and asks them the same
    # questions, where this compares text against a project that is
    # not in this repo.
    ("StorePath", "__copy__"): (
        "__copy__",
        "a value COPIES. Without it copy.copy falls through to pickle, "
        "which a bound C++ type cannot do - verified: the built module "
        "raises TypeError where the Cython one hands back a copy."),
    ("StorePath", "__deepcopy__"): (
        "__deepcopy__",
        "as __copy__. A bound value is immutable, so a deep copy IS a "
        "copy."),
    ("StorePath", "__repr__"): (
        "base_name=",
        "a repr names its fields. The declaration says the one field is "
        "called base_name and is read by to_string; a repr built from "
        "the accessor alone drops the name, and the Cython backend "
        "prints it - so one declaration was answering twice."),
    ("ValidPathInfo", "__copy__"): ("__copy__", "as StorePath's."),
    ("ValidPathInfo", "__deepcopy__"): ("__deepcopy__", "as StorePath's."),
    ("ValidPathInfo", "__repr__"): ("store_path=", "as StorePath's."),
    ("StorePath", "__gt__"): ("nb::is_operator()", "ordering, as __lt__."),
    ("StorePath", "__ge__"): ("nb::is_operator()", "ordering, as __lt__."),
    # Settled 2026-08-26. A remote store is opened over the network,
    # so the call is potentially slow and the GIL must not be held
    # across it. The flock argument pointed the same way - opening a
    # LocalStore waits on the temp-roots lock, and nanopynix's own
    # comment says `lockFile` calls checkInterrupt only AFTER flock
    # returns, so a wait holding the GIL would be uninterruptible -
    # but the network case is the one that decides it and needs no
    # reproduction to believe.
    ("open_store", "*"): (
        "nb::call_guard<nb::gil_scoped_release>()",
        "opening a remote store goes over the network, so holding the GIL "
        "across it stalls every other Python thread. nanopynix binds no "
        "guard here yet."),
    ("StorePath", "to_string"): (
        "&nix::StorePath::to_string",
        "bound by method pointer. <nanobind/stl/string_view.h> copies into a "
        "Python str, which is the copy the hand-written lambda was making, "
        "so the lambda hid the method behind a closure for nothing."),
    ("StorePath", "name"): ("&nix::StorePath::name", "as to_string."),
    ("StorePath", "hash_part"): ("&nix::StorePath::hashPart", "as to_string."),
}

# The two projects disagree, and only a person can settle it.
DIVERGENT = {
    # Settled 2026-08-26: follow Nix unless there is a reason not to.
    # nix/store/path.hh:45 declares `StorePath(std::string_view
    # baseName)`, so `base_name` is the name upstream gave it and the
    # declaration keeps it. `path` also reads badly beside the class's
    # own `name()` accessor, which returns the part after the hash.
    #
    # This stays DIVERGENT rather than becoming a BETTER claim because
    # nothing here can fix it: nanopynix's public API says `path`
    # today, and changing it is a breaking change in another repo.
    ("StorePath", "__init__"):
        "nanopynix names the constructor parameter `path`; upstream Nix "
        "calls it `baseName` (nix/store/path.hh:45), so the declaration "
        "says `base_name`. Renaming nanopynix's would break its callers, "
        "so this is a note rather than a diff to close. The docstrings "
        "differ too, and the declaration's is the one that will survive.",
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


def free_defs() -> dict[str, list[str]]:
    """Every module-level `m.def` in nanopynix, by Python name.

    A list per name, because these overload: `open_store` is
    registered twice, once with a uri and once without, and nanobind
    picks by argument type at call time."""
    out: dict[str, list[str]] = {}
    for src in sorted(NANOPYNIX.glob("*.cpp")):
        for m in re.finditer(r'\bm\.def\("([^"]+)",(.*?)\);',
                             strip_comments(src.read_text()), re.S):
            out.setdefault(m.group(1), []).append(
                " ".join(f'm.def("{m.group(1)}",{m.group(2)});'.split()))
    return out


def check_functions(decl_path: str) -> list[str]:
    """Free bindings, against nanopynix's module-level m.def calls."""
    mod = read(decl_path)
    if not mod.functions:
        return []
    want = free_defs()
    problems = []
    for fn in mod.functions:
        theirs = want.get(fn.name)
        if not theirs:
            print(f"  {fn.name}: SKIPPED - not in nanopynix")
            continue
        ours = " ".join(nbemit.free_function(fn)).strip()
        # Overloads share a name, so a match against ANY registration
        # is the honest test - the declaration lists each arity and
        # nanobind resolves them by type at call time.
        # The guard sits mid-list when arguments follow it and last
        # when none do, so drop it by pattern rather than by literal -
        # the literal-with-trailing-comma version silently failed to
        # strip the no-argument overload and reported a false diff.
        def nogil(t: str) -> str:
            return re.sub(r",?\s*nb::call_guard<nb::gil_scoped_release>\(\)",
                          "", t)

        stripped = [nogil(t) for t in theirs]
        bare = nogil(ours)
        if bare in stripped:
            print(f"  {fn.name}: matches a nanopynix registration")
            pinned = BETTER.get((fn.name, "*"))
            if pinned:
                pin, why = pinned
                if pin not in ours:
                    problems.append(
                        f"  {fn.name}: claimed better, but the emitted line "
                        f"no longer contains {pin!r}.")
                else:
                    print(f"    EMITTER IS RIGHT ({pin}). {why}")
            continue
        problems.append(f"  {fn.name}:")
        problems.append(f"    nanopynix: {theirs[0][:150]}")
        problems.append(f"    emitted:   {ours[:150]}")
    return problems


def check(decl_path: str) -> list[str]:
    mod = read(decl_path)
    problems = []
    for cls in mod.classes:
        want = hand_written(cls.name)
        if not want:
            print(f"  {cls.name}: SKIPPED - not in nanopynix")
            continue
        got = accessors(strip_comments(nbemit.bind_function(cls)))
        c = nbemit.census(cls)
        print(f"  {cls.name}: {len(got)} emitted, {len(want)} hand-written "
              f"({c['derived']} derived, {c['hatched']} hatched)")

        # Every claim of "the emitter is right", first and on its own.
        for (kls, name), (pin, why) in BETTER.items():
            if kls != cls.name:
                continue
            line = got.get(name)
            if line is None:
                problems.append(f"  {cls.name}.{name}: claimed better, but "
                                f"the emitter no longer binds it at all.")
            elif pin not in line:
                problems.append(
                    f"  {cls.name}.{name}: claimed better, but the emitted "
                    f"line no longer contains {pin!r}. Either the emitter "
                    f"regressed or the claim is stale.")
            else:
                print(f"    {name}: EMITTER IS RIGHT ({pin}). {why}")

        for name in sorted(set(want) | set(got)):
            a, b = want.get(name), got.get(name)
            for (kls, old), new in COSMETIC.items():
                if kls == cls.name and a:
                    a = re.sub(rf"\b{re.escape(old)}\b", new, a)
            if a == b:
                continue
            if (cls.name, name) in BETTER:
                continue  # already checked, above and unconditionally
            if (cls.name, name) in DIVERGENT:
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
    names = sys.argv[1:] or ["path", "pathinfo", "storefns"]
    print(f"emitted nanobind vs {NANOPYNIX}")
    problems = []
    for n in names:
        decl = cythonix_idl.declaration(n)
        problems += check(decl)
        problems += check_functions(decl)
    if problems:
        print("\n".join(["", "FAILED:", *problems]))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
