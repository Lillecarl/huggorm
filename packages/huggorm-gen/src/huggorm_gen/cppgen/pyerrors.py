"""Declaration -> the exception module, and the translator's catch chain.

Two outputs from one file, and they used to be two hand-written files
that had to agree. A Python class with no catch clause could never be
raised; a catch clause naming a class the module did not define failed
at import. Nothing checked either way.

The module is a TRANSFORM, like `pyenum.py`: a declaration of
exception classes IS the module, once the `cxx = "nix::..."` lines
come off. Every docstring, every base and the order are the
declaration's own nodes. It stays a transform because an exception
class body is arbitrary Python - methods, `__init__` - and not a
binding fact the model could hold.

The chain renders `ir.Errors`, which the reader's exception classes
build. Its order is the part a person gets wrong: C++ picks the FIRST
matching catch, so a base listed before its subclass swallows it.
The model computes it from the inheritance.

The module's BODY is `read.resolved`, not a raw parse (huggorm#73). A
raw `tree.body` holds both arms of an `if NIX_VERSION >= ...`, so the
transform would copy the whole branch into the emitted file. The
emitted module holds the classes this build's Nix has, flat, with no
condition left to evaluate and no `NIX_VERSION` to import.
"""

import ast
import copy

from huggorm_dsl.read import CXX, DECLARATIONS, HEADER, READER, DeclarationError
from huggorm_gen import ir

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


def _body(tree: ast.Module) -> list[ast.stmt]:
    """The declaration's statements, and a refusal if a branch is left.

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


def chain(errors: ir.Errors, raise_as: str) -> list[str]:
    """The translator's catch chain, most-derived first.

    `raise_as` is the C++ helper that sets the Python error: it is the
    one part of this that is not derived, because turning a
    `std::exception` into a live Python exception is nanobind's
    protocol rather than anything a declaration knows.

    `errors.module` is where the helper looks the class up. Passed
    from here, so the helper names no part of the library it raises
    into: a stale copy of the name fails at RUNTIME, by falling back
    to RuntimeError (huggorm#63).

    A class with no `cxx` is not caught. That is how a Python-only
    exception - one this binding raises itself and Nix never throws -
    stays in the module without inventing a catch for it.
    """
    out = ["    try {", "        throw;"]
    for name in errors.caught:
        error = errors.classes[name]
        readers = "".join(f", {r}" for r in error.readers)
        out.append(f"    }} catch (const {error.cxx} & e) {{")
        out.append(f'        {raise_as}("{errors.module}", "{name}", '
                   f"e{readers});")
    out.append("    }")
    return out


def _emitted_import(node: ast.stmt, package: str) -> ast.stmt:
    """An import of another declaration, pointed at what it emitted.

    `from huggorm_decl.decl.path import ErrorInfo` names the type a
    part carries. The emitted module runs where the declarations are
    not installed, and the class it means is the one `path` emitted."""
    if not (isinstance(node, ast.ImportFrom) and node.module
            and node.module.startswith(f"{DECLARATIONS}.")):
        return node
    if not package:
        raise DeclarationError(
            node, f"`{node.module}` is a declaration, and nothing says "
                  f"which package its emitted module is in.")
    out = copy.deepcopy(node)
    out.module = f"{package}.{node.module.removeprefix(f'{DECLARATIONS}.')}"
    return out


def module(tree: ast.Module, doc: str, package: str = "") -> str:
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
        node = _emitted_import(node, package)
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
            # huggorm#90.
            cls = copy.deepcopy(node)
            cls.body = [item for item in cls.body
                        if not (isinstance(item, ast.Assign)
                                and len(item.targets) == 1
                                and isinstance(item.targets[0], ast.Name)
                                and item.targets[0].id in (CXX, HEADER, READER))]
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

