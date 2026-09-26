"""Declaration set -> the bindings package's front door.

`from huggorm_bindings import StorePath` was twenty-five hand-written
re-export lines that tracked the declarations by hand. That is a
MAPPING - a Python name to the module it comes from - and it is the
same species as a hand-written C++ mapping, in a language where it
looks harmless (tasks/064). Four lines of it were added by hand in one
session for one new declaration, which is how the cost shows up.

Everything the file says is already in the declarations:

- WHICH names, from the classes each declaration binds, its free
  functions, and each vocabulary's words.
- WHERE each one lives, from the declaration's own stem, which is the
  same fact `generate.py` uses to name the `.cpp` beside it.

One name is derived by SUBTRACTION and it is the only rule here that
is not a list. A free function a class names with `@produced(by=...)`
is that class's constructor: `open_store` builds a Store, so a caller
types `Store("auto")` and never the factory's name.

That subtraction is not a convention this file invented. `nbemit`
binds a producer as `Store._ctor_from` and as NO module-level
function, so `huggorm_bindings.store` has no `open_store` in it at
all - measured, by dropping the subtraction and watching the emitted
front door fail to import. The hand-written file omitted the name
too, and had nothing that could say why.

The docstring below is the one hand-written thing left, and it is
prose rather than a mapping. It lives here because the file it
describes is generated, so there is nowhere in that file to keep it.
"""

import ast

from huggorm_dsl.corpus import Corpus
from huggorm_gen.cppgen import nbemit

# What `huggorm_bindings/__init__.py` says about itself. Prose only:
# every name and every import below it is derived.
DOC = '''
nanobind bindings over Nix, none of them hand-written.

There will be a lot of these, and most of them exist only so that
something else can be bound. An intermediate type is not scaffolding
to be thrown together: it is the surface everything above it sees, and
the place a mistake in it surfaces is three layers away.

## Where things come from

Nothing in here is the binding. Every module's C++ is written into the
build's copy of this directory from a declaration in
`huggorm-decl/src/huggorm_decl/decl/`, and `setup.py` compiles one
nanobind extension per declaration.

One Nix header, one declaration, named after it. `nix/store/path.hh`
is `decl/path.py` and compiles to `path`; `nix/store/store-api.hh` is
`decl/store.py` and compiles to `store`. Nix's own layout is the map,
so nobody has to learn a second one.

## What IS hand-written

Nothing, this file included. It used to be twenty-five re-export lines
that tracked the declarations by hand, and they are derived now: the
names come from what each declaration binds and the modules come from
the declarations' own stems (tasks/064).

The C++ this repo writes is not here either. It is
`huggorm_decl/cpp/`, with the declarations that NAME it - `@binds`
points at a function in there. See its README for the rule, and for
the sharper rule about what does not belong.

`errors.py` is emitted too, from `decl/errors.py`. It is pure Python
on purpose: a compiled module would need a reason, and a class
statement is not one.

There are no markers left. `_errors_module` and `_async_twins` stood
here and both are gone: the emitter that writes `errors.py` decides
where it goes, and a word's async spelling sits beside its C++ one in
`declare.py`. Nothing in this file is read by the generator now.

## What is declared and has no name of its own

`open_store`. `Store` names it with `@produced(by="open_store")`,
which makes it the store's constructor - so a caller writes
`Store("auto")`, and the factory is bound as `Store._ctor_from`
rather than as a function of its own.

## The mock

There isn't one. `fake-library/` was a C++ stand-in this repo grew
before real Nix was linked; every module here binds libstore or
libexpr now, and the stand-in is deleted (tasks/060).
'''


def exports(have: Corpus) -> dict[str, list[str]]:
    """Every name the package offers, by the module it comes from.

    Three sources and one subtraction.

    A declaration's CLASSES and its exported FREE FUNCTIONS come from
    the module it compiles to, named after the declaration's stem -
    the same fact that names the `.cpp` file.

    A VOCABULARY's words come from the plain-Python module the enum
    emitter writes, which is named the same way. They are not classes
    a binding compiles, so they are read from the vocabulary group
    rather than found among the rest.

    Then a FACTORY is dropped, and it has to be. `nbemit` binds one as
    its class's constructor and as no module-level function, so a
    front door naming it does not merely offer a second spelling -
    it fails to import. Measured: keeping it makes the emitted package
    raise `cannot import name 'open_store'`.

    `nbemit.public` decides which functions those are, and this reads
    it rather than restating it. The rule has an edge a copy misses:
    only a factory whose class declares a constructor is dropped.
    `parse_store_reference` makes a `StoreReference`, which has none,
    so it stays a module function, and the stub and `__all__` must
    both say so.
    """
    out: dict[str, list[str]] = {}
    for mod in have.modules:
        names = [c.name for c in mod.classes]
        names += [f.name for f in nbemit.public(mod.exported, mod.classes)]
        if names:
            out[mod.name] = sorted(names)
    for name in have.vocabularies:
        mod = have.module(name)
        words = [c.name for c in mod.classes if c.is_words]
        if words:
            out.setdefault(mod.name, []).extend(sorted(words))
    return {m: sorted(out[m]) for m in sorted(out)}


def module(have: Corpus) -> str:
    """The package's `__init__.py`, as source.

    Built with `ast` rather than by formatting strings, like every
    other emitted Python here: an import is a node, and a node cannot
    be malformed.

    `__all__` is sorted flat and the imports are grouped by module,
    which is the shape the hand-written file had - ruff keeps an
    `__all__` sorted, and the grouping that matters is which
    declaration a name came from.

    Plain imports, not `X as X`. The redundant spelling is what tells
    a typechecker a name is re-exported when there is no `__all__`;
    there is one here, and it says the same thing once.
    """
    by_module = exports(have)
    body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=DOC))]
    for name, names in by_module.items():
        body.append(ast.ImportFrom(
            module=name,
            names=[ast.alias(name=n) for n in names],
            level=1))
    everything = sorted({n for names in by_module.values() for n in names})
    body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=n) for n in everything])))
    out = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(out)
    return ast.unparse(out)
