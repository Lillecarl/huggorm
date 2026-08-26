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

from cythonix_idl import declare
from cythonix_idl.declare import Cxx, Decl

# Decorators that are Python's, not ours. A declaration may use them
# and they are read rather than applied.
BUILTIN_DECORATORS = frozenset({"property", "staticmethod", "classmethod",
                                "overload"})


# Where a declaration takes its vocabulary from. Named once: a
# declaration is read rather than imported, so this string is the only
# thing tying the two files together.
VOCABULARY = "cythonix_idl.declare"


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
    # This type names another DECLARED class rather than a primitive.
    # The emitter resolves the C++ spelling through the other
    # declaration, so neither file repeats it.
    bound: bool = False

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
class Param:
    """One declared parameter: its name, its type, and its default.

    `default` is the Python source of the default expression, or None
    when there is none. A binding that drops a default silently
    changes the Python signature, so it is read rather than ignored."""

    name: str
    type: Type
    default: str | None = None

    def __iter__(self):
        """Unpacks as `(name, type)`.

        Every emitter reads a parameter as that pair, and a default is
        a fourth thing only two of them care about. Rather than churn
        each call site into `pr.name, pr.type`, the pair stays the
        parameter's shape and the default is an attribute beside it."""
        return iter((self.name, self.type))


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
    params: tuple[Param, ...]
    ret: Type | None
    cxx_name: str = ""
    blocks: bool = False
    # This one cannot wait, on a class whose calls generally can.
    instant: bool = False
    # Declared with Python's own @property: an ATTRIBUTE, not a call.
    # A value's parts read as attributes and a handle's actions read
    # as methods, and which one an accessor is was never a property
    # of its class - nanopynix presents StorePath.to_string() as a
    # method and ValidPathInfo.path as an attribute, and both are
    # values. So the declaration says it, in the word Python already
    # has for it.
    prop: bool = False
    # The C++ function a FREE function binds, from @binds.
    binds: str = ""
    # One of several registrations under one Python name, from
    # typing.@overload. nanobind resolves them by argument type at
    # call time, so the declaration lists each arity it accepts.
    overload: bool = False
    # The C++ data member behind this name, from @reads. Empty when
    # the accessor is a call rather than a field.
    reads: str = ""
    # Verbatim C++ for an accessor nothing can derive, from @cxx_body.
    cxx_body: str = ""
    # The call that produces a POD, from @cxx_parts. One or more C++
    # statements; the emitter writes the struct and the return around
    # them.
    parts_prelude: str = ""
    # Field name -> the C++ expression that yields it. A tuple rather
    # than a dict because a Method is frozen and hashable, and a dict
    # member is neither.
    parts: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Member:
    """One word of a vocabulary: the name, the string, and why.

    `doc` is the docstring written UNDER the assignment, which is
    where Python puts an attribute's own documentation. Empty for a
    member whose name says the whole of it."""

    name: str
    value: str
    doc: str = ""


@dataclass(frozen=True)
class Class:
    """One declared class: what it says, and what its decorators said."""

    name: str
    doc: str
    decl: Decl
    ctor: Method | None
    methods: tuple[Method, ...] = ()
    # The words, for a vocabulary. Empty for every other kind.
    members: tuple[Member, ...] = ()

    @property
    def is_words(self) -> bool:
        """Whether this is a vocabulary rather than a binding.

        A StrEnum with no C++ object behind it. It crosses as the
        string a member already IS, so nothing about it compiles."""
        return self.decl.kind == "words"

    @property
    def is_value(self) -> bool:
        """Whether this class holds Python slots and no C++ at all.

        Two facts, not one, and an earlier version read `@produced`
        as if it were both. `@produced(by=...)` says only that nothing
        constructs one. `@binding(cxx=...)` says there is a C++ object
        behind it. PathInfo has the first and not the second, so it is
        a value; nix::Store has both, so it is a handle a factory
        opens - and emitting it as a value produced a module with the
        class in it twice."""
        return bool(self.decl.built_by) and not self.decl.cxx


@dataclass(frozen=True)
class Module:
    """One declaration file."""

    name: str
    doc: str
    classes: tuple[Class, ...] = ()
    functions: tuple[Method, ...] = ()
    # Local name -> name in declare. `from declare import Str as S`
    # is legal Python, so the reader follows the import rather than
    # matching the spelling it expects.
    vocabulary: dict[str, str] = field(default_factory=dict)


# -- the vocabulary -------------------------------------------------------

def _vocabulary(tree: ast.Module) -> dict[str, str]:
    """Every name this file took from declare, as local -> declared.

    Only the vocabulary module counts. A declaration that imports
    anything else is not refused here - it may import for a type
    checker's sake - but nothing outside the vocabulary can decorate
    or annotate, and the resolvers below say so."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == VOCABULARY:
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


def type_of(node: ast.expr, vocab: dict[str, str]) -> Type:
    """One annotation, resolved.

    Three answers, and the reader gives whichever the annotation
    supports rather than deciding per class. An earlier version had a
    per-class flag for this and it was wrong twice: first it keyed off
    `@produced`, which made nix::Store's C++ parameters Python; then
    off `wire`, which did the same to nix::StorePath. The fact was
    never a property of the class.

    A name in the vocabulary carries a C++ spelling. A capitalised
    name that is not vocabulary refers to another declared class.
    Anything else is a plain Python type, which is the whole truth
    about a field the C++ side already flattened.

    What this does NOT do is guess. A bare `str` where C++ is needed
    reaches an emitter with `cxx=None`, and the emitter refuses it
    there - which is the right place, because only the emitter knows
    whether it needed one."""
    # A string annotation is the forward reference a declaration needs
    # to name a type declared in another file. Unwrapped here so the
    # rest of the reader sees one spelling.
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        spelled = node.value
    else:
        spelled = ast.unparse(node)
    if isinstance(node, ast.Name) and node.id in vocab:
        alias = _from_vocabulary(node.id, vocab, node)
        if get_origin(alias) is not None:
            for meta in get_args(alias)[1:]:
                if isinstance(meta, Cxx):
                    # The PYTHON spelling of an alias is its first
                    # arg: `Annotated[str, Cxx("string_view")]` is a
                    # str. QUALIFIED when it is not a builtin, because
                    # `Path` alone is ambiguous and `pathlib.Path` is
                    # what an annotation has to say to typecheck.
                    inner = get_args(alias)[0]
                    name = inner.__name__
                    if inner.__module__ != "builtins":
                        name = f"{inner.__module__}.{name}"
                    return Type(python=name, cxx=meta)
        raise DeclarationError(
            node, f"'{node.id}' is vocabulary but carries no C++ spelling. "
                  f"Annotate the alias with Cxx(...) in declare.py.")
    # The reader records a reference to another declared class;
    # resolving it needs that declaration, which only the emitter has.
    bare = spelled.replace(" | None", "").removeprefix("list[").rstrip("]")
    if bare[:1].isupper():
        return Type(python=spelled, bound=True)
    return Type(python=spelled)


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
            if node.id in BUILTIN_DECORATORS:
                # Python's own words, and they mean here what they
                # mean everywhere. @property says an accessor is an
                # attribute rather than a call.
                continue
            target = _from_vocabulary(node.id, vocab, node)(target)
        else:
            raise DeclarationError(node, f"cannot read @{ast.unparse(node)}")
    return target


# -- methods --------------------------------------------------------------

def _method(node: ast.FunctionDef, vocab: dict[str, str],
            bound: bool = True) -> Method:
    """One declared function.

    `bound=False` for a module-level one, which has no `self` to skip.
    Reading a free function as a method silently drops its first
    parameter, which is how `open_store(uri)` first emitted without
    the `"uri"_a` that makes the parameter usable by keyword."""
    args = node.args
    if args.vararg or args.kwarg or args.kwonlyargs or args.posonlyargs:
        raise DeclarationError(
            node, f"{node.name}: a bound method takes plain positional "
                  f"parameters. C++ has no *args.")
    # Defaults bind to the LAST parameters, so line them up from the
    # right - `f(a, b=1)` has one default and it belongs to b.
    positional = args.args[1:] if bound else args.args
    pad = len(positional) - len(args.defaults)
    params = []
    for i, arg in enumerate(positional):
        if arg.annotation is None:
            raise DeclarationError(
                arg, f"{node.name}({arg.arg}): every parameter states its "
                     f"type.")
        d = args.defaults[i - pad] if i >= pad else None
        params.append(Param(arg.arg, type_of(arg.annotation, vocab),
                            ast.unparse(d) if d is not None else None))

    ret: Type | None = None
    returns = node.returns
    if returns is not None and not (isinstance(returns, ast.Constant)
                                    and returns.value is None):
        ret = type_of(returns, vocab)

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
        instant=bool(getattr(marked, "_instant", False)),
        prop=any(isinstance(d, ast.Name) and d.id == "property"
                 for d in node.decorator_list),
        binds=getattr(marked, "_binds", ""),
        overload=any(isinstance(d, ast.Name) and d.id == "overload"
                     for d in node.decorator_list),
        reads=getattr(marked, "_reads", ""),
        cxx_body=getattr(marked, "_cxx_body", ""),
        parts_prelude=getattr(marked, "_cxx_parts", ("", {}))[0],
        parts=tuple(getattr(marked, "_cxx_parts", ("", {}))[1].items()),
    )


def _members(node: ast.ClassDef) -> tuple[Member, ...]:
    """The words of a vocabulary, in the order it lists them.

    `NAME = "value"`, and the docstring that may follow it. Python
    puts an attribute's documentation under the assignment rather
    than inside it, so the two are read as one pair here."""
    out: list[Member] = []
    for i, item in enumerate(node.body):
        if not isinstance(item, ast.Assign):
            continue
        if len(item.targets) != 1 or not isinstance(item.targets[0], ast.Name):
            raise DeclarationError(item, "a word is one plain assignment")
        if not (isinstance(item.value, ast.Constant)
                and isinstance(item.value.value, str)):
            raise DeclarationError(
                item, "a word IS a string. Nothing else crosses as one.")
        doc = ""
        nxt = node.body[i + 1] if i + 1 < len(node.body) else None
        if (isinstance(nxt, ast.Expr) and isinstance(nxt.value, ast.Constant)
                and isinstance(nxt.value.value, str)):
            doc = nxt.value.value
        out.append(Member(targets_name(item), item.value.value, doc))
    if not out:
        raise DeclarationError(node, f"{node.name}: a vocabulary with no words")
    return tuple(out)


def targets_name(item: ast.Assign) -> str:
    target = item.targets[0]
    assert isinstance(target, ast.Name)
    return target.id


def _class(node: ast.ClassDef, vocab: dict[str, str]) -> Class:
    if node.bases:
        raise DeclarationError(
            node, f"{node.name}: this spike declares no inheritance.")
    holder = _apply(node.decorator_list, vocab, type(node.name, (), {}))
    decl: Decl = holder.__dict__.get("_decl", Decl())
    decl.name = node.name

    if decl.kind == "words":
        return Class(
            name=node.name,
            doc=ast.get_docstring(node, clean=False) or "",
            decl=decl,
            ctor=None,
            members=_members(node),
        )

    ctor: Method | None = None
    methods: list[Method] = []
    for item in node.body:
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name == "__init__":
            ctor = _method(item, vocab)
        elif not item.name.startswith("_"):
            # Definition order, which is the order a reader of the
            # declaration sees and the order the emitted file keeps.
            methods.append(_method(item, vocab))
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
    # Module-level functions are FREE bindings - nanopynix has 72 of
    # them, `m.def("open_store", &open_store_uri, "uri"_a)` and its
    # kind. Only decorated ones: an undecorated def at module level is
    # a helper the declaration wrote for itself.
    functions = tuple(_method(n, vocab, bound=False) for n in tree.body
                      if isinstance(n, ast.FunctionDef) and n.decorator_list)
    return Module(
        name=pathlib.Path(path).stem,
        doc=ast.get_docstring(tree, clean=False) or "",
        classes=classes,
        functions=functions,
        vocabulary=vocab,
    )
