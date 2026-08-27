"""
A declaration file, read WITHOUT running it.

`ast.parse` turns the file into a tree. Nothing in it executes: no
import, no decorator call, no class body. So a declaration can name a
C++ type this machine has never compiled, and reading it costs a
parse.

## Why this matters more than it looks

The generator used to build its manifest by IMPORTING the compiled
bindings and reflecting on them. That works, and it puts the whole
build in one order: compile the C++ first, learn what it says second.
Every surface above - async, protocols, RPC, stubs - waited on a C++
compiler.

Reading the declaration inverts that. The manifest is known BEFORE
anything compiles, because the declaration already said everything
the manifest holds. Then the binding and the manifest are two
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
import pathlib
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

# Where the declarations live. A declaration that names a type
# another declaration owns imports it from here, and the reader
# follows that import rather than being told the file.
DECLARATIONS = "cythonix_idl.decl"


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
    # Headers this method's BODY needs, beyond its class's, from
    # @needs. Empty when the signature already names everything.
    headers: tuple[str, ...] = ()
    # The call that produces a POD, from @cxx_parts. One or more C++
    # statements; the emitter writes the struct and the return around
    # them.
    parts_prelude: str = ""
    # Field name -> the C++ expression that yields it. A tuple rather
    # than a dict because a Method is frozen and hashable, and a dict
    # member is neither.
    parts: tuple[tuple[str, str], ...] = ()
    # A C++ method a Python subclass may override, from @virtual. What
    # makes a trampoline necessary and what says which methods it
    # forwards.
    virtual: bool = False
    # ...and with no implementation to fall back to, from @pure.
    pure: bool = False
    # Run once at module import, and do not export, from @startup.
    startup: bool = False
    # Register as the module's exception translator, from @translator.
    translator: bool = False
    # The threading policy a FREE function opts into, from @threading.
    # Empty means it declared none, which is what keeps a runtime
    # helper out of every generated form.
    policy: str = ""


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
    # The declaration file that declared it, without the suffix. The
    # emitters name a module after its declaration, so this is also
    # the module the class lands in - and a class that arrived
    # through `uses` carries the OTHER file's name, which is the only
    # way an emitter can write the import that reaches it.
    module: str = ""

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
    # Classes this declaration NAMES but does not declare, from
    # another declaration it imported. A vocabulary lives in its own
    # file and several bindings take one, so the alternative was
    # every emitter guessing which other files to read.
    uses: dict[str, Class] = field(default_factory=dict)

    @property
    def startup(self) -> tuple[Method, ...]:
        """What runs once when the module is imported."""
        return tuple(fn for fn in self.functions if fn.startup)

    @property
    def translators(self) -> tuple[Method, ...]:
        """The C++ that turns a library exception into a Python one."""
        return tuple(fn for fn in self.functions if fn.translator)

    @property
    def exported(self) -> tuple[Method, ...]:
        """The free functions the module actually offers a caller.

        A startup hook and a translator are declared here because
        this is where a module's C++ facts live, and neither is
        surface: one runs before a caller exists and the other runs
        instead of one."""
        return tuple(fn for fn in self.functions
                     if not (fn.startup or fn.translator))

    @property
    def known(self) -> dict[str, Class]:
        """Every class this declaration can name, by name."""
        return {**self.uses, **{c.name: c for c in self.classes}}
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
        headers=tuple(getattr(marked, "_needs", ())),
        parts_prelude=getattr(marked, "_cxx_parts", ("", {}))[0],
        parts=tuple(getattr(marked, "_cxx_parts", ("", {}))[1].items()),
        virtual=bool(getattr(marked, "_virtual", False)),
        pure=bool(getattr(marked, "_pure", False)),
        startup=bool(getattr(marked, "_startup", False)),
        translator=bool(getattr(marked, "_translator", False)),
        policy=getattr(marked, "_policy", ""),
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


def _class(node: ast.ClassDef, vocab: dict[str, str],
           where: str = "", live: set[int] | None = None) -> Class:
    if node.bases:
        raise DeclarationError(
            node, f"{node.name}: a declaration states its base with "
                  f"@derives, not as a Python base class. A Python "
                  f"hierarchy here would be one among objects that are "
                  f"never constructed, and the two would drift.")
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
            module=where,
        )

    ctor: Method | None = None
    methods: list[Method] = []
    # ...with any `NIX_VERSION` branch already chosen by the import.
    for item in _resolve(node.body, live):
        if not isinstance(item, ast.FunctionDef):
            continue
        if item.name == "__init__":
            ctor = _method(item, vocab)
            if decl.built_by and ctor.params:
                # A `@produced(by=X)` class is built by X, so X owns
                # the signature. Declaring it twice is how the
                # `uri="auto"` default died: `open_store` carried it,
                # `__init__` did not, and the emitter read the wrong
                # one - so `Store()` raised, `AsyncStore()` required
                # an argument, and the stub and the manifest agreed
                # with each other about the wrong thing.
                #
                # The `__init__` is still worth writing: it is where
                # the PROSE goes, and a caller reading the declaration
                # looks for it under the name they will call. Only the
                # parameters are refused.
                raise DeclarationError(
                    item,
                    f"{node.name}.__init__ declares parameters, but "
                    f"@produced(by={decl.built_by!r}) says "
                    f"{decl.built_by} builds one - so {decl.built_by} "
                    f"owns the signature. Move them there and leave "
                    f"the docstring here.")
        elif not item.name.startswith("__"):
            # Definition order, which is the order a reader of the
            # declaration sees and the order the emitted file keeps.
            methods.append(_method(item, vocab))
    return Class(
        name=node.name,
        doc=ast.get_docstring(node, clean=False) or "",
        decl=decl,
        ctor=ctor,
        methods=tuple(methods),
        module=where,
    )


def _live(path: str) -> set[int] | None:
    """The first line of every class and function the IMPORT kept.

    A declaration may branch on `NIX_VERSION`, and Python resolves
    that during the import. This asks the resulting module which
    definitions survived, and the answer is a set of LINES.

    Lines, not names: both arms of an `if` define the same name, so a
    name match keeps both and the emitter binds the method twice.
    `co_firstlineno` is the first DECORATOR's line when a definition
    has decorators and the `def`/`class` line when it has none, so a
    caller matches either.

    `None` when the file will not import. That is not a failure here:
    a declaration is a document first, and one that cannot be
    imported still parses - so the reader falls back to the tree
    alone and every `if` arm is read. `_resolve` says what that
    costs."""
    import importlib.util

    name = f"_cythonix_decl_{pathlib.Path(path).stem}"
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:
        return None

    lines: set[int] = set()
    for obj in vars(mod).values():
        code = getattr(obj, "__code__", None)
        if code is not None:
            lines.add(code.co_firstlineno)
        elif isinstance(obj, type) and obj.__module__ == name:
            lines.add(getattr(obj, "__firstlineno__", 0))
            for member in vars(obj).values():
                inner = getattr(member, "__code__", None)
                if inner is not None:
                    lines.add(inner.co_firstlineno)
    lines.discard(0)
    return lines


def _resolve(body: list[ast.stmt], live: set[int] | None) -> list[ast.stmt]:
    """One body with its `if` arms already chosen.

    Nothing here evaluates a condition. Python did that during the
    import, and this keeps what Python kept - matched by the line a
    definition starts on.

    With `live` unknown the `if` is flattened whole, which reads both
    arms and will usually declare a name twice. That is loud rather
    than silent: the emitter binds it twice and the build says so."""
    out: list[ast.stmt] = []

    def walk(nodes: list[ast.stmt]) -> None:
        for n in nodes:
            if isinstance(n, ast.If):
                walk(n.body)
                walk(n.orelse)
            elif isinstance(n, ast.ClassDef | ast.FunctionDef):
                own = {n.lineno} | {d.lineno for d in n.decorator_list}
                if live is None or own & live:
                    out.append(n)
            else:
                out.append(n)

    walk(body)
    return out


def read(path: str) -> Module:
    """One declaration file, read twice.

    `ast.parse` gives the tree, which is what every emitter reads and
    what `pyi.py` transforms. The IMPORT gives one fact the tree
    cannot: which definitions survive a `NIX_VERSION` branch.

    The import is the authority on WHAT exists. The tree is the source
    for HOW to render it. No fact is taken from both."""
    source = pathlib.Path(path).read_text()
    tree = ast.parse(source, filename=path)
    live = _live(path)
    body = _resolve(tree.body, live)
    vocab = _vocabulary(tree)
    stem = pathlib.Path(path).stem
    classes = tuple(_class(n, vocab, stem, live) for n in body
                    if isinstance(n, ast.ClassDef) and n.decorator_list)
    # Module-level functions are FREE bindings - nanopynix has 72 of
    # them, `m.def("open_store", &open_store_uri, "uri"_a)` and its
    # kind. Only decorated ones: an undecorated def at module level is
    # a helper the declaration wrote for itself.
    functions = tuple(_method(n, vocab, bound=False) for n in body
                      if isinstance(n, ast.FunctionDef) and n.decorator_list)
    return Module(
        name=stem,
        doc=ast.get_docstring(tree, clean=False) or "",
        classes=classes,
        functions=functions,
        vocabulary=vocab,
        uses=_uses(tree, pathlib.Path(path).parent),
    )


def _uses(tree: ast.Module, here: pathlib.Path) -> dict[str, Class]:
    """Declarations this one imported, read.

    `from cythonix_idl.decl.content_address import ContentAddressMethod`
    is how a declaration names a type another declaration owns. The
    import is never executed - nothing here is - but it is the one
    place that says WHICH other file to read, so following it beats
    every emitter holding a list of declarations to try."""
    out: dict[str, Class] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if not (node.module or "").startswith(f"{DECLARATIONS}."):
            continue
        stem = (node.module or "").rsplit(".", 1)[-1]
        source = here / f"{stem}.py"
        if not source.exists():
            raise DeclarationError(
                node, f"no declaration at {source}. A declaration may only "
                      f"import another declaration beside it.")
        for cls in read(str(source)).classes:
            out[cls.name] = cls
    return out
