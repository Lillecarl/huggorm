"""
A declaration file, read WITHOUT running it.

`ast.parse` turns the file into a tree. Nothing in it executes: no
import, no decorator call, no class body. So a declaration can name a
C++ type this machine has never compiled, and reading it costs a
parse.

## Why this matters more than it looks

The current generator builds its manifest by IMPORTING the compiled
bindings and reflecting on them. That works, and it puts the whole
build in one order: compile the C++ first, learn what it says second.
Every surface above - async, protocols, RPC, stubs - waits on a C++
compiler.

Reading the declaration instead inverts that. The manifest is known
BEFORE anything compiles, because the declaration already said
everything the manifest holds. Then the .pyx and the manifest are two
readings of one document rather than two stages of a pipeline.

## What still executes

`declare.py` does. It is the vocabulary, not a declaration: a library
of decorators and dataclasses with no side effects, which this module
imports the way a type checker would.

And it earns its import. A decorator's job is to write a field on a
`Decl`, so rather than restate that mapping here - `header` sets
`.header`, `produced(by=)` sets `.built_by` - this module APPLIES the
real decorator to a throwaway object and reads the result. The
mapping lives in one place, which is declare.py, and a decorator that
gains an argument needs no edit here.

## What it refuses

A name it cannot resolve, an annotation with no C++ spelling, a
decorator that is not from the vocabulary. Each stops with the line
number and the text that caused it. Guessing here would produce a
binding that compiles and is wrong.
"""

import ast
from dataclasses import dataclass, field
from typing import Any, get_args, get_origin

import declare
from declare import Cxx, Decl


class DeclarationError(Exception):
    """A declaration this reader will not guess at.

    Carries the line, because the reader's answer to an unreadable
    declaration is to point at it."""

    def __init__(self, node: ast.AST, message: str) -> None:
        line = getattr(node, "lineno", "?")
        super().__init__(f"line {line}: {message}")


@dataclass(frozen=True)
class Type:
    """A declared type, from both sides of the boundary.

    `python` is what the annotation said, verbatim. `cxx` is the C++
    fact behind it, and it is None for a PRODUCED value - such a class
    holds no C++ object at all, so its fields are already Python and
    there is nothing for a C++ spelling to describe."""

    python: str
    cxx: Cxx | None = None

    @property
    def wire(self) -> str:
        """The `_wire_fields` spelling of this type.

        Derived, not declared. `T | None` is `T?`, because that is how
        the wire says presence; everything else crosses as itself."""
        inner, optional = self.python, False
        if inner.endswith("| None"):
            inner, optional = inner[:-len("| None")].strip(), True
        return f"{inner}?" if optional else inner


@dataclass(frozen=True)
class Method:
    """One declared method, with the C++ facts resolved.

    `params` and `ret` hold `Cxx`, not names: resolution happens once,
    here, so no emitter downstream has to know that `StrView` means
    `string_view` and owes the boundary a copy."""

    name: str
    # RAW, as written. Cleaning is a reader's convenience and an
    # emitter's problem: the manifest wants the literal text a class
    # carries, and `_doc` re-indents from raw anyway.
    doc: str
    params: tuple[tuple[str, Type], ...]
    ret: Type | None
    cxx_name: str = ""
    blocks: bool = False
    # The C++ data member behind this name, from @reads. Empty when
    # the accessor is a call rather than a field.
    reads: str = ""
    # Verbatim C++ for an accessor nothing can derive, from @cxx_body.
    cxx_body: str = ""


@dataclass(frozen=True)
class Class:
    """One declared class: what it says, and what its decorators said."""

    name: str
    doc: str
    decl: Decl
    ctor: Method | None
    methods: tuple[Method, ...] = ()


@dataclass(frozen=True)
class Module:
    """One declaration file."""

    name: str
    doc: str
    classes: tuple[Class, ...] = ()
    # Local name -> name in declare. `from declare import Str as S`
    # is legal Python, so the reader follows the import rather than
    # matching the spelling it expects.
    vocabulary: dict[str, str] = field(default_factory=dict)


# -- the vocabulary -------------------------------------------------------

def _vocabulary(tree: ast.Module) -> dict[str, str]:
    """Every name this file took from declare, as local -> declared.

    Only `from declare import ...` counts. A declaration that imports
    anything else is not refused here - it may import for a type
    checker's sake - but nothing outside the vocabulary can decorate
    or annotate, and the resolvers below say so."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "declare":
            for alias in node.names:
                out[alias.asname or alias.name] = alias.name
    return out


def _from_vocabulary(name: str, vocab: dict[str, str], node: ast.AST) -> Any:
    if name not in vocab:
        raise DeclarationError(
            node, f"'{name}' is not from declare. A declaration may only use "
                  f"the vocabulary it imported.")
    obj = getattr(declare, vocab[name], None)
    if obj is None:
        raise DeclarationError(
            node, f"declare has no '{vocab[name]}'. The import is stale.")
    return obj


def type_of(node: ast.expr, vocab: dict[str, str], cxx: bool) -> Type:
    """One annotation, resolved.

    `cxx=True` for a class that binds a C++ object: then every type
    must carry a C++ spelling, because a bare `str` leaves this
    guessing between std::string and string_view - and those differ by
    whether the boundary owes the value a copy, which is the
    difference between a correct binding and a dangling pointer.

    `cxx=False` for a PRODUCED value. Nothing there crosses from C++:
    the object that made it flattened one, and what is left is Python
    slots. So a plain annotation is not a gap in the declaration, it
    is the whole truth about the field."""
    # A string annotation is the forward reference a declaration needs
    # to name a type declared in another file. Unwrapped here so the
    # rest of the reader sees one spelling.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        spelled = node.value
    else:
        spelled = ast.unparse(node)
    if not cxx:
        return Type(python=spelled)
    if not isinstance(node, ast.Name):
        raise DeclarationError(
            node, f"'{spelled}' is not a declared type name. Give it an "
                  f"Annotated alias in declare.py.")
    alias = _from_vocabulary(node.id, vocab, node)
    if get_origin(alias) is not None:
        for meta in get_args(alias)[1:]:
            if isinstance(meta, Cxx):
                # The PYTHON spelling of an alias is its first arg:
                # `Annotated[str, Cxx("string_view")]` is a str.
                return Type(python=get_args(alias)[0].__name__, cxx=meta)
    raise DeclarationError(
        node, f"'{node.id}' carries no C++ spelling. Annotate the alias with "
              f"Cxx(...) in declare.py.")


# -- literals -------------------------------------------------------------

def _value(node: ast.expr, vocab: dict[str, str]) -> Any:
    """A decorator argument, as the object it denotes.

    Literals go through ast.literal_eval, which evaluates no code. A
    call is allowed only when it names something in the vocabulary -
    `Field("base_name", "str", read="to_string")` - and then the real
    Field is built, so its own defaults and validation apply."""
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise DeclarationError(node, f"cannot read {ast.unparse(node)}")
        target = _from_vocabulary(node.func.id, vocab, node)
        args = [_value(a, vocab) for a in node.args]
        kwargs = {k.arg: _value(k.value, vocab)
                  for k in node.keywords if k.arg is not None}
        return target(*args, **kwargs)
    if isinstance(node, ast.Tuple):
        return tuple(_value(e, vocab) for e in node.elts)
    try:
        return ast.literal_eval(node)
    except ValueError as exc:
        raise DeclarationError(
            node, f"'{ast.unparse(node)}' is not a constant. A declaration "
                  f"holds facts, not expressions.") from exc


# -- decorators -----------------------------------------------------------

def _apply(decorators: list[ast.expr], vocab: dict[str, str],
           target: Any) -> Any:
    """Run the file's decorators against a stand-in.

    The declaration's own class is never built. What gets decorated is
    a throwaway that carries nothing, so the only thing that happens
    is declare.py writing fields onto it - which is the mapping this
    reader would otherwise have to restate and keep in step.

    Bottom-up, like Python: `@header` above `@binding` means binding
    applies first, and a decorator that overwrote a field would win in
    the same order a reader expects."""
    for node in reversed(decorators):
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise DeclarationError(node, f"cannot read @{ast.unparse(node)}")
            fn = _from_vocabulary(node.func.id, vocab, node)
            args = [_value(a, vocab) for a in node.args]
            kwargs = {k.arg: _value(k.value, vocab)
                      for k in node.keywords if k.arg is not None}
            target = fn(*args, **kwargs)(target)
        elif isinstance(node, ast.Name):
            target = _from_vocabulary(node.id, vocab, node)(target)
        else:
            raise DeclarationError(node, f"cannot read @{ast.unparse(node)}")
    return target


# -- methods --------------------------------------------------------------

def _method(node: ast.FunctionDef, vocab: dict[str, str],
            cxx: bool = True) -> Method:
    args = node.args
    if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
        raise DeclarationError(
            node, f"{node.name}: a bound method takes plain positional "
                  f"parameters. C++ has no *args.")
    params = []
    for arg in args.args[1:]:
        if arg.annotation is None:
            raise DeclarationError(
                arg, f"{node.name}({arg.arg}): every parameter states its "
                     f"type.")
        params.append((arg.arg, type_of(arg.annotation, vocab, cxx)))

    ret: Type | None = None
    returns = node.returns
    if returns is not None and not (isinstance(returns, ast.Constant)
                                    and returns.value is None):
        ret = type_of(returns, vocab, cxx)

    # A method decorator writes an attribute on a function, so the
    # same trick works: decorate a stand-in and read what was written.
    def probe() -> None: ...
    marked = _apply(node.decorator_list, vocab, probe)
    return Method(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        params=tuple(params),
        ret=ret,
        cxx_name=getattr(marked, "_cxx_name", ""),
        blocks=bool(getattr(marked, "_blocks", False)),
        reads=getattr(marked, "_reads", ""),
        cxx_body=getattr(marked, "_cxx_body", ""),
    )


def _class(node: ast.ClassDef, vocab: dict[str, str]) -> Class:
    if node.bases:
        raise DeclarationError(
            node, f"{node.name}: this spike declares no inheritance.")
    holder = _apply(node.decorator_list, vocab, type(node.name, (), {}))
    decl: Decl = holder.__dict__.get("_decl", Decl())
    decl.name = node.name
    # A produced value binds nothing, so nothing it names needs a C++
    # spelling. `built_by` is what @produced writes, and it is the one
    # fact that decides which vocabulary the annotations are in.
    cxx = not decl.built_by

    ctor: Method | None = None
    methods: list[Method] = []
    for item in node.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name == "__init__":
            ctor = _method(item, vocab, cxx)
        elif not item.name.startswith("_"):
            # Definition order, which is the order a reader of the
            # declaration sees and the order the emitted file keeps.
            methods.append(_method(item, vocab, cxx))
    return Class(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        decl=decl,
        ctor=ctor,
        methods=tuple(methods),
    )


def read(path: str) -> Module:
    """One declaration file, parsed."""
    import pathlib
    source = pathlib.Path(path).read_text()
    tree = ast.parse(source, filename=path)
    vocab = _vocabulary(tree)
    classes = tuple(_class(n, vocab) for n in tree.body
                    if isinstance(n, ast.ClassDef) and n.decorator_list)
    return Module(
        name=pathlib.Path(path).stem,
        doc=ast.get_docstring(tree, clean=False) or "",
        classes=classes,
        vocabulary=vocab,
    )
