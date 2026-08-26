"""
The gate. What the declaration produces, against what the repo has.

Two claims, checked the same way: emit from the declaration, diff
against the artefact the real build produced by another route, and
print what does not match.

1. **The Cython.** Each declared class against the hand-written class
   in `cythonix-bindings`. Comments and docstring wording differ,
   because a human wrote those and the emitter did not. What may NOT
   differ is a single line of code, so this compares the two with
   comments and docstrings stripped.

   This half SHRINKS as the spike wins. `path` has already left it:
   the build emits its three files from `decl/path.py` and the repo
   holds none of them, so there is nothing to diff and the claim is
   proven by the compiler and the suite instead. A module still
   listed here is a module the emitter has not taken over yet.

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
import ast
import json
import pathlib
import re
import sys

import cythonix_idl
from cythonix_idl import emit, manifest, pyi
from cythonix_idl.generate import MODULES
from cythonix_idl.read import read

# The repo root: this file sits in cythonix-idl/gates/.
ROOT = pathlib.Path(__file__).resolve().parents[2]
BINDINGS = ROOT / "cythonix-bindings" / "cythonix_bindings"

# Differences that are not differences, each with the reason it is
# not one. Listed and PRINTED rather than normalised away: the same
# bargain the custom hatch makes, because an allowance nobody counts
# is where the real divergence goes to hide.
# Empty, and that is the point. Every entry this held was about a
# class the emitter has since taken over, so the difference it
# excused stopped existing: there is no hand-written PathInfo left to
# name a local `info` where the emitter names it `out`.
#
# Kept as a mechanism rather than deleted, because the next
# declaration to arrive will need it before it needs anything else.
ACCEPTED: tuple[tuple[str, str, str, str], ...] = (
    ("Store", "store", "c_self",
     "the local holding the store's pointer. The human named it after "
     "the type; the emitter names every hoisted receiver the same, "
     "because it writes one for every blocking call and a name per "
     "type would be a table to keep."),
    ("Store", "sp", "b_path",
     "the local a Python-annotated parameter is typed through. The "
     "human named it after the type; the emitter names it after the "
     "parameter, like every other local it writes."),
    ("Store", "p", "c_path",
     "the local holding a parameter's pointer, named after the "
     "parameter it came from rather than abbreviated."),
    ("store.hpp", "store", "s",
     "the shim's own parameter. Every emitted shim names its receiver "
     "the same, because the emitter writes the body's one reference to "
     "it as well."),
    ("store.hpp", "const nix::Store &", "nix::Store &",
     "constness. The declaration does not carry whether a call only "
     "reads, and a non-const reference binds to the object this "
     "binding holds in every case - so the emitter writes the one "
     "spelling that always compiles rather than guessing the one that "
     "documents better."),
    ("store.hpp", "store_uri", "get_uri",
     "the shim's name, as above."),
    ("Store", "store_uri", "get_uri",
     "the shim's name. The emitter WRITES the shim now, so it names it "
     "after the method it backs; a second name for one call was a "
     "thing to keep in step."),
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


_SAID: set[tuple[str, str]] = set()


def allow(want: list[str], target: str) -> list[str]:
    """Apply this target's allowances to the repo's side.

    An identifier is replaced on a word boundary, and anything else
    must match a whole statement. A plain substring replace is what
    the first version did, and it rewrote `query_path_info` into
    `query_path_out` - an allowance quietly editing code it was never
    meant to touch is worse than no allowance."""
    return _apply(want, target, whole=True)


def _edged(text: str) -> str:
    """A pattern that matches `text` and not a longer name around it.

    `\b` only where the edge is a word character. An allowance for
    `const nix::Store &` ends in `&`, and `\b` after a `&` matches
    nothing - so a naive word-boundary pattern silently never fires
    and the allowance reads as applied while doing nothing."""
    left = r"\b" if text[:1].isalnum() or text[:1] == "_" else ""
    right = r"\b" if text[-1:].isalnum() or text[-1:] == "_" else ""
    return left + re.escape(text) + right


def _apply(want: list[str], target: str, whole: bool) -> list[str]:
    for name, theirs, ours, why in ACCEPTED:
        if name != target:
            continue
        # Once per target, not once per call. check_methods calls this
        # for every method, and an allowance printed eighteen times
        # buries the diff it is meant to explain.
        if (name, theirs) not in _SAID:
            _SAID.add((name, theirs))
            print(f"  {target}: allowing '{theirs}' -> '{ours}': {why}")
        if whole and not theirs.isidentifier() and " " not in theirs:
            want = [ours if line == theirs else line for line in want]
        else:
            want = [re.sub(_edged(theirs), ours, line) for line in want]
    return want


def allow_text(text: str, target: str) -> str:
    """The same allowances, over raw source rather than statements.

    The C++ half needs them applied BEFORE it can find anything: the
    shim this repo wrote by hand is called `store_uri` and the one
    the emitter writes is called `get_uri`, so a lookup by the
    declared name finds nothing until the rename has happened."""
    return "\n".join(_apply(text.splitlines(), target, whole=False))


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
        if not cls.is_value:
            # Holds a C++ object, so it is not a produced VALUE - it
            # is a handle with no constructor. `@produced` says only
            # "nothing constructs one"; whether there is C++ behind it
            # is `@binding(cxx=...)`, and the two facts are separate.
            problems += check_methods(decl_path, pyx_name, cls.name)
            problems += check_shim(decl_path, f"{mod.name}.hpp", cls.name)
            problems += check_pod_pxd(decl_path, f"c_{mod.name}.pxd", cls.name)
            continue
        want = code_only(class_body(actual, cls.name))
        if not want:
            # The same story as path's, one class at a time. This
            # class is emitted into a .pxi that the build writes and
            # `{pyx_name}` includes, so there is no hand-written copy
            # to diff. The manifest below still checks it, and that
            # check now closes a loop: the declaration wrote the
            # source, the compiler compiled it, reflection read the
            # .so back, and the two manifests must still agree.
            print(f"  {cls.name}: BUILT FROM THE DECLARATION - not in "
                  f"{pyx_name}, which includes the emitted .pxi instead.")
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


def method_body(text: str, cls_name: str, name: str) -> str:
    """One `def` out of a hand-written class.

    A class the emitter has not taken over yet is not all-or-nothing.
    Comparing per method is what lets a declaration grow one method at
    a time and still be checked at every step - and the count it
    prints is the honest measure of how far the emitter has got."""
    body = class_body(text, cls_name).splitlines()
    start = next((i for i, line in enumerate(body)
                  if line.strip().startswith(f"def {name}(")), None)
    if start is None:
        return ""
    indent = len(body[start]) - len(body[start].lstrip())
    out = [body[start]]
    for line in body[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


def _settled(lines: list[str]) -> list[str]:
    """Statements with the opening run of `cdef` locals sorted.

    Order is code, and this comparison keeps it - except across the
    declarations a method opens with. Those are independent of each
    other by construction: each one converts one argument, none reads
    another, and the hand-written file itself writes them in two
    different orders in two neighbouring methods. Sorting exactly that
    run says the emitter must produce the same locals, and need not
    have guessed which of two arbitrary orders a human picked."""
    # Past the `def` line first: the run this sorts is the one that
    # opens the BODY.
    start = 1 if lines and lines[0].startswith("def ") else 0
    head = start
    while head < len(lines) and lines[head].startswith("cdef "):
        head += 1
    return lines[:start] + sorted(lines[start:head]) + lines[head:]


def check_methods(decl_path: pathlib.Path, pyx_name: str,
                  cls_name: str) -> list[str]:
    """A class still written by hand, one declared method at a time.

    Reports a score rather than a pass. A method the declaration does
    not carry yet is not a failure - it is work not done - but a
    method it DOES carry and gets wrong is."""
    mod = read(str(decl_path))
    actual = (BINDINGS / pyx_name).read_text()
    cls = next(c for c in mod.classes if c.name == cls_name)
    # The produced values this module declares, by name. A method that
    # returns one needs its FIELDS to emit the unpacking, so the
    # emitter is handed the map rather than reading the file again.
    values = {c.name: c for c in mod.classes if c.is_value}
    hand = [line.strip()[4:].split("(")[0]
            for line in class_body(actual, cls_name).splitlines()
            if line.strip().startswith("def ")]
    named = {m.name for m in cls.methods}
    # Split three ways, because one number was read as two. "8 of 8
    # declared, 18 written by hand" says nothing about how much is
    # left: the 18 is every `def` in the class, the 8 declared ones
    # included, and a dunder the declaration will never carry sits in
    # it too.
    dunders = [n for n in hand if n.startswith("__")]
    todo = [n for n in hand if n not in named and not n.startswith("__")]
    problems, agree = [], 0
    for m in cls.methods:
        want = allow(code_only(method_body(actual, cls_name, m.name)), cls_name)
        if not want:
            problems.append(f"{cls_name}.{m.name}: declared, but "
                            f"{pyx_name} has no such method")
            continue
        got = code_only("\n".join(
            emit._accessor(m, cls.decl.blocking, cls, values)))
        want, got = _settled(want), _settled(got)
        if want == got:
            agree += 1
            continue
        problems.append(f"{cls_name}.{m.name}: emitted code differs")
        # Reported from the SETTLED lists, and paired by position when
        # the two are the same length. A report that prints only what
        # is missing from the other side says nothing at all when the
        # difference is an order, which is exactly the case this
        # comparison had to think about.
        if len(want) == len(got):
            for a, b in zip(want, got, strict=True):
                if a != b:
                    problems.append(f"    the repo: {a}")
                    problems.append(f"    emitted:  {b}")
            continue
        for line in want:
            if line not in got:
                problems.append(f"    only in the repo: {line}")
        for line in got:
            if line not in want:
                problems.append(f"    only emitted:     {line}")
    print(f"  {cls_name}: {agree} of {len(cls.methods)} declared methods "
          f"identical, {len(todo)} still to declare"
          + (f", plus {', '.join(dunders)}" if dunders else ""))
    if todo:
        print(f"    still hand-written: {', '.join(todo)}")
    return problems


def cxx_only(text: str) -> list[str]:
    """C++ reduced to the statements a compiler reads.

    The same bargain `code_only` makes, in the other language. A
    `/** */` block carries a human's reasons, and the emitter writes
    its own from the declaration's docstring - so neither is what
    "the same shim" means."""
    out: list[str] = []
    in_doc = False
    for raw in text.splitlines():
        line = raw.strip()
        if in_doc:
            if line.endswith("*/"):
                in_doc = False
            continue
        if line.startswith("/*"):
            if not line.endswith("*/"):
                in_doc = True
            continue
        if not line or line.startswith("//"):
            continue
        out.append(re.sub(r"\s+", " ", line))
    return out


def shim_body(text: str, name: str) -> str:
    """One `inline` function out of the hand-written header."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.startswith("inline ") and f" {name}(" in line), None)
    if start is None:
        return ""
    out, depth, opened = [], 0, False
    for line in lines[start:]:
        out.append(line)
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth == 0:
            break
    return "\n".join(out)


def struct_body(text: str, name: str) -> str:
    """One `struct` out of the hand-written header."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip() == f"struct {name}"), None)
    if start is None:
        return ""
    out, depth, opened = [], 0, False
    for line in lines[start:]:
        out.append(line)
        depth += line.count("{") - line.count("}")
        opened = opened or "{" in line
        if opened and depth == 0:
            break
    return "\n".join(out)


def check_shim(decl_path: pathlib.Path, hpp_name: str,
               cls_name: str) -> list[str]:
    """The emitted C++ against the C++ this repo wrote by hand.

    The other half of the same claim. `check_methods` says the
    emitter writes the Cython; this says it writes the C++ underneath
    it, which is the half `_cpp/store.hpp` has held by hand since
    tasks/015."""
    mod = read(str(decl_path))
    cls = next(c for c in mod.classes if c.name == cls_name)
    actual = allow_text((BINDINGS / "_cpp" / hpp_name).read_text(), hpp_name)
    values = {c.name: c for c in mod.classes if c.is_value}
    problems, agree, declared = [], 0, 0
    # The POD each produced value crosses in. Derived whole from that
    # value's own fields, so nothing declares it - which is exactly
    # why it is worth diffing against the struct this repo compiles.
    for name in dict.fromkeys(m.ret.python for m in cls.methods
                              if m.parts and m.ret is not None):
        want = cxx_only(struct_body(actual, f"{name}Parts"))
        got = cxx_only("\n".join(emit.pod_struct(values[name])))
        if not want:
            problems.append(f"{hpp_name}: no struct named {name}Parts")
            continue
        declared += 1
        if want == got:
            agree += 1
            continue
        problems.append(f"{hpp_name}:{name}Parts: the emitted struct differs")
        for a_, b_ in zip(want, got, strict=False):
            if a_ != b_:
                problems += [f"    the repo: {a_}", f"    emitted:  {b_}"]
    # The two helpers the emitter writes for itself, checked like any
    # other. They are not declared anywhere, so nothing else would
    # notice if they stopped matching the C++ this repo compiles.
    for helper in ("store_path_set", "base_names", "to_strings"):
        want = cxx_only(shim_body(actual, helper))
        got = cxx_only(shim_body(emit.FLATTENING, helper))
        if not want:
            continue
        declared += 1
        if want == got:
            agree += 1
            continue
        problems.append(f"{hpp_name}:{helper}: the emitter's copy differs")
        for a, b in zip(want, got, strict=False):
            if a != b:
                problems += [f"    the repo: {a}", f"    emitted:  {b}"]
    for m in cls.methods:
        if not emit.shimmed(m):
            continue
        declared += 1
        want = cxx_only(shim_body(actual, m.name))
        if not want:
            problems.append(f"{hpp_name}: no shim named {m.name}")
            continue
        body = ("\n".join(emit._parts_body(cls, m, values)) if m.parts
                else m.cxx_body.strip())
        got = cxx_only(emit._shim_signature(cls, m, values)
                       + "\n{\n" + body + "\n}")
        if want == got:
            agree += 1
            continue
        problems.append(f"{hpp_name}:{m.name}: emitted C++ differs")
        for a, b in zip(want, got, strict=False):
            if a != b:
                problems.append(f"    the repo: {a}")
                problems.append(f"    emitted:  {b}")
    print(f"  {hpp_name}: {agree} of {declared} declared shims identical")
    return problems


def check_pod_pxd(decl_path: pathlib.Path, pxd_name: str,
                  cls_name: str) -> list[str]:
    """The third artefact of a POD crossing, against the repo's pxd.

    The struct is declared twice by necessity - once in C++ and once
    for Cython - and the two must agree member for member or the
    binding reads one field as another. Both come from the value's own
    fields here, so the gate is what says the repo's pair still does.
    """
    mod = read(str(decl_path))
    cls = next(c for c in mod.classes if c.name == cls_name)
    values = {c.name: c for c in mod.classes if c.is_value}
    actual = (BINDINGS / pxd_name).read_text()
    problems, agree, declared = [], 0, 0
    for name in dict.fromkeys(m.ret.python for m in cls.methods
                              if m.parts and m.ret is not None):
        want = code_only(pxd_struct(actual, f"C{name}"))
        got = code_only("\n".join(emit.pod_pxd(values[name])))
        if not want:
            problems.append(f"{pxd_name}: no struct named C{name}")
            continue
        declared += 1
        if want == got:
            agree += 1
            continue
        problems.append(f"{pxd_name}:C{name}: the emitted struct differs")
        for a_, b_ in zip(want, got, strict=False):
            if a_ != b_:
                problems += [f"    the repo: {a_}", f"    emitted:  {b_}"]
    print(f"  {pxd_name}: {agree} of {declared} POD declarations identical")
    return problems


def pxd_struct(text: str, name: str) -> str:
    """One `cdef struct` out of a hand-written pxd."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines)
                  if line.strip().startswith(f"cdef struct {name} ")), None)
    if start is None:
        return ""
    indent = len(lines[start]) - len(lines[start].lstrip())
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        out.append(line)
    return "\n".join(out)


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
    missing = [f for f in files if not (BINDINGS / f).exists()]
    if len(missing) == len(files):
        # The whole module is emitted by the build now. There is no
        # hand-written file left to diff against, and inventing one
        # would only compare the emitter to itself. What proves this
        # module instead is downstream: it compiles, the manifest
        # below agrees with what reflection found in the compiled
        # .so, and the suite passes against it.
        print(f"  {mod.name}: BUILT FROM THE DECLARATION - "
              f"{', '.join(sorted(files))} are not in the repo. "
              f"Proven by the build, not by a diff.")
        return []
    for fname, emitted in files.items():
        actual = BINDINGS / fname
        if not actual.exists():
            problems.append(f"{fname}: nothing to compare against, but "
                            f"{sorted(set(files) - set(missing))} is - so "
                            f"this module is half generated and half not")
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
#
# `PathInfo.deriver` was here and is not any more. It was annotated
# `-> StorePath` in the hand-written pyx while its own wire_fields
# said `StorePath?`, so the two routes disagreed. The emitter now
# WRITES that annotation, from the same wire_fields, so the .so
# reflects `StorePath | None` and the disagreement is gone. That is
# the spike paying out: a bug found by diffing two routes, then
# closed by making one of them the source. See tasks/052.
KNOWN_WRONG_RETURNS = {
    ("PathInfo", "ca"): (
        "typing.Union[str, None]", "str | None",
        "one type, two spellings. model.py reads a typing object and "
        "renders it; the declaration says what the source said."),
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
        declared = {m["name"] for m in got["methods"]}
        reflected = {m["name"] for m in want["methods"]}
        if declared < reflected:
            # Half a declaration cannot produce a whole manifest
            # entry, and reporting the missing half as a disagreement
            # would say the emitter is WRONG about methods it has not
            # been told about yet. The per-method gate above is what
            # measures a class at this stage; this one waits for the
            # last method.
            print(f"  {cls.name}: manifest DEFERRED - "
                  f"{len(declared)} of {len(reflected)} methods declared. "
                  f"Missing: {', '.join(sorted(reflected - declared))}")
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


# A stub the transform writes and the string emitter does not, with
# the reason. The transform WINS here: it moves the declaration's own
# nodes, so a docstring the declaration wrote arrives intact, where a
# printer has to be told to carry it and was not.
STUB_BETTER = (
    ('"""Raises when the name is not a store path. The message comes',
     "the constructor's docstring. The declaration wrote it and the "
     "transform moves the node, so it arrives; the string emitter "
     "builds a signature and drops the body it came from."),
)


def check_stub(decl_path: pathlib.Path, stub_path: pathlib.Path) -> list[str]:
    """The stub a TRANSFORM produces, against the one a printer did.

    The other emitters build text because they have to - a `.pyx` has
    no AST and a nanobind module body is one expression. A `.pyi` is
    Python, and the declaration is already Python, so `pyi.py` edits
    the declaration's tree and unparses it instead.

    This says the two agree. Where they do not, the difference is
    listed above with the reason, and every one so far is the
    transform keeping something the printer dropped."""
    mod = read(str(decl_path))
    tree = ast.parse(decl_path.read_text())
    got = pyi.stub(mod, tree, "x").splitlines()[1:]
    want = stub_path.read_text().splitlines()
    # Past the shipped module docstring, which is the generator's own
    # sentence about itself rather than anything the declaration said.
    while want and not want[0].startswith("class "):
        want.pop(0)
    while got and not got[0].startswith("class "):
        got.pop(0)
    extra = [line for line in got if line not in want]
    missing = [line for line in want if line not in got]
    problems = []
    for line in extra:
        why = next((w for start, w in STUB_BETTER
                    if line.strip().startswith(start)), "")
        if why:
            print(f"    {line.strip()[:40]}...: THE TRANSFORM IS RIGHT. {why}")
        elif line.strip() and not line.strip().startswith(('"""', "from ")):
            problems.append(f"    only from the transform: {line}")
    for line in missing:
        problems.append(f"    only from the string emitter: {line}")
    print(f"  {stub_path.name}: {len(want)} lines, "
          f"{len(want) - len(missing)} of them from the transform too")
    return problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("declaration", nargs="*", default=["path", "store"])
    ap.add_argument("--manifest", default="")
    args = ap.parse_args()
    problems = []
    for name in args.declaration:
        decl = pathlib.Path(cythonix_idl.declaration(name))
        print(f"{decl.name} (parse only - it never runs)")
        print("  emitted Cython vs the repo's:")
        problems += check_cython(decl)
        if args.manifest:
            print("  declared manifest entry vs the reflected one:")
            problems += check_manifest(decl, pathlib.Path(args.manifest))
            stub = (pathlib.Path(args.manifest).parent.parent
                    / "cythonix_bindings-stubs" / f"{name}.pyi")
            # Only where the declaration owns the WHOLE module. A stub
            # holds every class in a module, so comparing one against
            # a declaration that covers part of it reports the rest as
            # missing - which is work not done rather than a
            # disagreement, and the per-method count above already
            # measures it.
            if stub.exists() and f"decl/{name}.py" in MODULES:
                print("  stub by TRANSFORM vs stub by string emitter:")
                problems += check_stub(decl, stub)
        else:
            print("  manifest: SKIPPED - pass --manifest <manifest.json>")

    if problems:
        print("\n".join(["", "FAILED:", *problems]))
        return 1
    print("\nOK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
