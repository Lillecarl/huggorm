"""Declaration -> the exception module, and the translator's catch chain.

Two outputs from one file, and they used to be two hand-written files
that had to agree. A Python class with no catch clause could never be
raised; a catch clause naming a class the module did not define failed
at import. Nothing checked either way.

The module is a TRANSFORM, like `pyenum.py`: a declaration of
exception classes IS the module, once the `cxx = "nix::..."` lines
come off. Every docstring, every base and the order are the
declaration's own nodes.

The chain is DERIVED, and the thing it derives is the part a person
gets wrong. C++ picks the FIRST matching catch, so a base listed
before its subclass swallows it - `nix::Error` first would make every
one of these a NixError. Python's own inheritance already says which
class derives from which, so the order is computed rather than
maintained.

All three readings take every `ast.ClassDef` in the body, with no
decorator test. That is what an error declaration is: `cxx =
"nix::Error"` is a bare assignment, because there is no behaviour to
mark. A `declared()` helper stood here that read `Module.classes`
instead - the reader's DECORATED classes - so it answered `[]` for
this file from the day it was written, and nothing ever called it.
Deleted rather than fixed (tasks/072), because there is nothing left
for it to check: the module and the chain come from ONE reading of
one file, so they cannot disagree.

The BODY is `read.resolved`, not a raw parse, and the difference is
`tasks/073`. A raw `tree.body` holds neither arm of an `if
NIX_VERSION >= ...` - the classes are nested one level down - so a
branched error class reached no manifest entry and no catch clause,
while `module()` copied the whole branch through into the emitted
file. Three holes, none of them loud. The reader has resolved a
version branch since it was written; this now asks it rather than
parsing the file again.

So a branch is a BUILD-TIME question here. The emitted module holds
the classes this build's Nix has, flat, with no condition left to
evaluate and no `NIX_VERSION` to import.
"""

import ast
import copy
from types import ModuleType
from typing import Any

from huggorm_dsl.read import DeclarationError

# The attribute a declared exception uses to name its C++ class. Not a
# decorator: an exception declaration has no behaviour to mark, and a
# bare assignment reads as the fact it is.
CXX = "cxx"
HEADER = "header"


# The package a declaration is WRITTEN in. Read off the reader's own
# module rather than spelled, so a rename of the language moves this
# with it.
LANGUAGE = DeclarationError.__module__.split(".")[0]


def _is_language_import(node: ast.stmt) -> bool:
    """Whether this statement imports the declaration language.

    `from huggorm_dsl.declare import NIX_VERSION` and its kind. True
    for the module itself and for anything under it, because a
    declaration reaches the vocabulary through `huggorm_dsl.declare`
    and could reach `NIX_VERSION` through either."""
    if isinstance(node, ast.ImportFrom):
        head = (node.module or "").split(".")[0]
        return head == LANGUAGE
    if isinstance(node, ast.Import):
        return any(a.name.split(".")[0] == LANGUAGE for a in node.names)
    return False


def _assigned(node: ast.ClassDef, name: str) -> str:
    """A bare `name = "..."` in this class body, or empty.

    Two lines are read this way and they are the same shape, so one
    reader answers both."""
    for item in node.body:
        if (isinstance(item, ast.Assign)
                and len(item.targets) == 1
                and isinstance(item.targets[0], ast.Name)
                and item.targets[0].id == name
                and isinstance(item.value, ast.Constant)):
            return str(item.value.value)
    return ""


def _cxx_of(node: ast.ClassDef) -> str:
    """The C++ class this exception stands for, from its `cxx` line."""
    return _assigned(node, CXX)


def _header_of(node: ast.ClassDef) -> str:
    """Where that C++ class is DECLARED, from its `header` line.

    The same fact `@header` carries for a bound class, in the
    spelling an error declaration uses. An error wears no decorator -
    there is no behaviour to mark - and `Union.header` already exists
    for that reason, so a bare assignment beside `cxx` is the
    consistent shape rather than a third idea.

    What it is FOR: the emitted translator catches
    `nix::BadStorePathName` and its kind, so the emitted file needs
    the headers that declare them. Those three includes sat in
    `cpp/errors.hpp` instead - a fact about generated code, stated in
    a hand-written helper, which is the wrong place for it
    (`tasks/090`)."""
    return _assigned(node, HEADER)


def headers(tree: ast.Module) -> list[str]:
    """Every header the catch chain needs, once each, sorted.

    Sorted rather than declared-order, because an include block is a
    set and the chain's order - most-derived first - is a fact about
    the CATCHES and not about the includes. Ordering them by
    declaration would make a reordered declaration rewrite an
    unrelated block.

    Refuses BOTH directions, because either one alone is a line that
    reaches nothing:

    - a `cxx` with no `header` is a catch whose type the emitted file
      can only reach by accident, through somebody else's transitive
      include. That is how these three came to live in `errors.hpp`.
    - a `header` with no `cxx` is a line no emitter reads, and a line
      nobody reads is indistinguishable from a line nobody wrote.
    """
    out: set[str] = set()
    for node in _body(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        cxx, header = _cxx_of(node), _header_of(node)
        if cxx and not header:
            raise DeclarationError(
                node, f"{node.name}: `cxx = \"{cxx}\"` says the emitted "
                      f"translator catches this type, and nothing says "
                      f"which header declares it. Add `header = \"nix/...\"` "
                      f"beside it, or the emitted file reaches the type "
                      f"only through somebody else's include (tasks/090).")
        if header and not cxx:
            raise DeclarationError(
                node, f"{node.name}: `header` with no `cxx`. Only a class "
                      f"the translator CATCHES needs a header emitted for "
                      f"it, so this line reaches no emitter - and a line "
                      f"nobody reads looks exactly like one nobody wrote.")
        if header:
            out.add(header)
    return sorted(out)


def _body(tree: ast.Module) -> list[ast.stmt]:
    """The declaration's statements, and a refusal if a branch is left.

    Every reading here goes through this, so all three see one list.
    They each walked `tree.body` on their own, which is how they came
    to disagree: two read the top level and the third copied the whole
    document (tasks/073).

    An `ast.If` means the caller passed a RAW parse. `read.resolved`
    flattens every branch - Python already chose an arm during the
    import - so a surviving `if` is not a declaration this emitter
    cannot handle, it is the wrong tree. Refused rather than walked
    into: walking it would answer for both arms of a branch, which is
    a build emitting a class its own Nix does not have.

    The check is the WIRING's gate. Nothing else can catch a caller
    reverting to `Corpus.tree`, because the two trees are identical
    for a declaration that does not branch - and this repo's does not,
    today."""
    for node in tree.body:
        if isinstance(node, ast.If):
            raise DeclarationError(
                node, "this exception declaration still has a version "
                      "branch in it. Read it with `read.resolved`, which "
                      "chooses the arm the import kept - a raw parse "
                      "answers for both.")
    return tree.body


def _bases(tree: ast.Module) -> dict[str, str]:
    """Each declared class to its declared base, by name."""
    return {n.name: (n.bases[0].id if n.bases
                     and isinstance(n.bases[0], ast.Name) else "")
            for n in _body(tree) if isinstance(n, ast.ClassDef)}


def _depth(name: str, bases: dict[str, str]) -> int:
    """How far this class is from the root of the declared hierarchy.

    The sort key for the catch chain. A subclass is deeper than its
    base, so ordering by depth descending puts every class before
    anything it derives from - which is what C++ needs, because it
    takes the first catch that matches."""
    seen, depth = set(), 0
    while name in bases and bases[name] and name not in seen:
        seen.add(name)
        name, depth = bases[name], depth + 1
    return depth


WIRE_FIELDS = "_wire_fields"


def entries(tree: ast.Module,
            mod: ModuleType) -> dict[str, dict[str, Any]]:
    """Every declared exception, as the manifest carries it.

    Two readings of one file, each answering what it is good for. The
    TREE says which classes this document declares and in what order.
    The IMPORT says what each one INHERITS, and that is the half a
    tree cannot give.

    `_wire_fields` is declared once, on NixError, and eight classes
    below it carry the same two strings. An earlier version of this
    walked the declared base chain to find them, which recomputed
    what Python had already computed while executing the file - and
    would have got it wrong the first time a hierarchy branched,
    because a tree walk follows the first base and an MRO does not.

    Bases INSIDE this file only. `Exception` is Python's and says
    nothing about the hierarchy a caller catches by; the reflected
    version this replaced meant the same thing by
    `b.__module__ == module_name`.

    A module, never None. This refused a missing one itself, on the
    reading that inventing a hierarchy would put a wrong answer in
    four generated files at once - which was right, and was the only
    place that asked. `load` refuses a declaration that will not
    import now, for every reader rather than this one, and says the
    reason Python gave rather than only that there was one
    (tasks/082).
    """
    declared = [n.name for n in _body(tree) if isinstance(n, ast.ClassDef)]
    here = mod.__name__
    out = {}
    for name in sorted(declared):
        kls = getattr(mod, name)
        out[name] = {
            "bases": [b.__name__ for b in kls.__bases__
                      if b.__module__ == here],
            # Through the MRO, so a class states its parts once and
            # every class below it carries them.
            "wire_fields": [list(f) for f in
                            getattr(kls, WIRE_FIELDS, ())],
        }
    return out


def chain(tree: ast.Module, raise_as: str, module: str) -> list[str]:
    """The translator's catch chain, most-derived first.

    `raise_as` is the C++ helper that sets the Python error: it is the
    one part of this that is not derived, because turning a
    `std::exception` into a live Python exception is nanobind's
    protocol rather than anything a declaration knows.

    `module` is where the helper looks the class up. It was a string
    literal inside the helper, which made it a copy of a name the
    build derives three other ways - and a copy that no gate could
    see, because a stale one fails at RUNTIME by falling back to
    RuntimeError (tasks/063). Passed from here, the helper names no
    part of the library it raises into, and both strings in the call
    come from the same declaration.

    A class with no `cxx` is skipped. That is how a Python-only
    exception - one this binding raises itself and Nix never throws -
    stays in the module without inventing a catch for it.
    """
    bases = _bases(tree)
    caught = [(n.name, _cxx_of(n)) for n in _body(tree)
              if isinstance(n, ast.ClassDef) and _cxx_of(n)]
    caught.sort(key=lambda pair: -_depth(pair[0], bases))
    out = ["    try {", "        throw;"]
    for name, cxx in caught:
        out.append(f"    }} catch (const {cxx} & e) {{")
        out.append(f'        {raise_as}("{module}", "{name}", e);')
    out.append("    }")
    return out


def module(tree: ast.Module, doc: str) -> str:
    """The exception module, as the declaration with its C++ taken off.

    A transform rather than a print, for `pyenum.py`'s reason: the
    output is Python and the declaration already is. What changes is
    only what a caller must not see - the `cxx` and `header` lines,
    which name C++ that no Python caller can act on, and any import
    of the declaration LANGUAGE.

    The language import goes because the reason for it is gone. A
    declaration that branches writes `from huggorm_dsl.declare import
    NIX_VERSION` to spell the condition, and `read.resolved` has
    already chosen the arm - so the name is unreferenced by the time
    this runs, and carrying it would make the emitted package import
    a build-time one. Anything else the declaration imports stays: a
    class body may legitimately name `typing`, and only the language
    is knowably build-time.

    Derived rather than spelled. The package is `read`'s own, which
    is the same module that resolved the branch, so there is no
    second place for the name to be wrong.
    """
    body: list[ast.stmt] = []
    for node in _body(tree):
        if _is_language_import(node):
            continue
        if isinstance(node, ast.ClassDef):
            # A COPY, because `corpus()` is cached for the process and
            # hands every emitter the same tree. Stripping in place
            # took `cxx` off that shared tree, so any reader that came
            # after this one saw a declaration with no C++ in it at
            # all - `chain` would emit a translator that catches
            # NOTHING, and `headers` an include block with nothing in
            # it. Both are silent: an empty chain compiles.
            #
            # It worked only because `generate.py` happens to call
            # `error_chain` before this. Found on 2026-09-05 by
            # `headers` being added and reading []; recorded in
            # `tasks/090`.
            cls = copy.deepcopy(node)
            cls.body = [item for item in cls.body
                        if not (isinstance(item, ast.Assign)
                                and len(item.targets) == 1
                                and isinstance(item.targets[0], ast.Name)
                                and item.targets[0].id in (CXX, HEADER))]
            cls.decorator_list = []
            body.append(cls)
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
            # The module docstring, replaced below.
            continue
        body.append(node)
    header = ast.Expr(value=ast.Constant(value=doc))
    out = ast.Module(body=[header, *body], type_ignores=[])
    ast.fix_missing_locations(out)
    return ast.unparse(out)

