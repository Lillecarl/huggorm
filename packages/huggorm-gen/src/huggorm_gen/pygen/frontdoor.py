"""Declarations -> `huggorm/__init__.py`, the library's front door.

The second of the two front doors, and the one that had a reason to
wait. `cppgen/pyinit.py` writes the bindings' own; this writes the
one a caller actually types `import huggorm` for (tasks/064).

Forty-odd `from huggorm_bindings import Hash as Hash` lines that had
to follow every declaration in the repo, and a test that said so
after the fact. The test stays and becomes a cross-check; the lines
are derived.

## What is derived, and from what

- Everything `huggorm_bindings` exports, which `cppgen/pyinit.py`
  already derives from the declarations. Read from there rather than
  computed again.
- Everything `huggorm_generated` exports, which `emitter.package_exports`
  already derives from the manifest. Read from there for the same
  reason. `RPC_CLASSES` is dropped: it is a registry the client uses
  to turn a handle into an object, not surface.
- The UNION aliases. They live in `_unions` because a union has no
  home in a compiled package - the alias is Python and the module
  binding its arms is an extension - and a caller annotating their
  own function with `DerivedPath` is the whole point of it having a
  name.

## What is NOT derived, and why that is right

The hand-written layer's own surface: `connect`, `NixClient`,
`ConnectionExpired`, `serve`, `errors`, `set_pool_size`. No
declaration names any of them, because they are not bindings - they
are the library this repo writes over the bindings. `LOCAL` below is
a short list, and it changes when that layer's API changes rather
than when a declaration does. That was the whole complaint: the
maintenance burden was tracking DECLARATIONS, and there were
thirty-odd of those.

## Where it lands

The build's copy only, and the tree keeps none. That is Carl's call,
and it costs the dev loop `nix run test` had: with no `__init__.py`
in `packages/huggorm/huggorm/`, that directory is a namespace portion
and Python's finder prefers the store's regular package - so the
whole of `huggorm/` comes from the store, not just this file.
Measured, not assumed. `nix run test` writes the file into the tree
before it runs pytest, which is the same thing both `setup.py` files
already do with their own output.
"""

import ast
from typing import Any

# The names this package offers that no declaration knows about.
#
# Its own layer: a client, a server, the exception module and the
# pool-size knob. Listed rather than derived because there is nothing
# to derive them FROM - they are hand-written Python, and a module
# that grew an `__all__` for this to read would be the same list in
# another file.
#
# Keyed by the module each comes from, the way the emitted imports
# are grouped. A relative name is relative to `huggorm` itself.
LOCAL: dict[str, list[str]] = {
    "huggorm_generated._runtime": ["set_pool_size"],
    ".remote": ["ConnectionExpired", "NixClient", "connect"],
    ".server": ["serve"],
    ".watch": ["Watcher"],
    ".notify": ["Notifier"],
}

# Imported as a MODULE rather than for the names in it. `huggorm.errors`
# is how a caller writes `except huggorm.errors.BadStorePath`, and the
# classes in it are the emitted hierarchy rather than a list this could
# carry.
LOCAL_MODULES: list[str] = ["errors"]

# Where the union aliases live. Not `huggorm_generated` itself: the
# package re-exports classes and functions, and an alias is neither.
UNIONS_MODULE = "huggorm_generated._unions"

BINDINGS = "huggorm_bindings"
GENERATED = "huggorm_generated"

# A registry the client reads to turn a handle into an object. The
# generated package exports it because it is a name in that package;
# the front door does not, because it is plumbing.
PLUMBING = frozenset({"RPC_CLASSES"})

DOC = '''
Nix, from Python.

    import huggorm

    store = huggorm.Store("auto")
    path = store.add_to_store("hello", b"hello\\n")
    for dep in store.compute_fs_closure([path]):
        print(dep)

Three packages build this library and one imports it. `huggorm` is
the front door; `huggorm_bindings` (compiled), `huggorm_generated`
(emitted at build time) and this one are how it is made, which is not
something a caller should have to learn before the first import.

## The three surfaces

The same store, spelled three ways, and the choice is about WHERE the
work happens rather than what it does.

- **Sync** - `Store`, `StorePath`, `PathInfo`. The bindings
  themselves. Every call blocks the calling thread.
- **Async** - `AsyncStore` and friends, from `connect_local`. The same
  calls, awaited, with the blocking part moved onto a thread so an
  event loop keeps running.
- **Remote** - `connect()` gives a client whose objects satisfy the
  same protocols (`StoreLike`) and run on another process's store.

A protocol is what both async surfaces promise, so code written
against `StoreLike` runs either way.

## What is NOT here

Nothing, now. The `Mock*` classes were the last exception and they are
gone (tasks/060). Every name the two packages behind this one export
reaches this front door - and it is derived from them now rather than
listed by hand, so a new binding arrives here without anyone
noticing (tasks/064).

`RPC_CLASSES` is the one name held back. It is a registry the remote
client reads to turn a handle into an object, so it is plumbing
rather than something to call.
'''


def exports(bindings: list[str], generated: list[str],
            unions: list[str]) -> dict[str, list[str]]:
    """Every name the front door offers, by the module it comes from.

    The three derived groups, then the hand-written layer's own. Each
    argument is a list somebody else already derived - this joins
    them and drops the plumbing, and states no name of its own beyond
    `LOCAL`.
    """
    out: dict[str, list[str]] = {
        BINDINGS: sorted(bindings),
        GENERATED: sorted(n for n in generated if n not in PLUMBING),
        UNIONS_MODULE: sorted(unions),
    }
    for module, names in LOCAL.items():
        out.setdefault(module, []).extend(names)
    # A name in both packages reaches the caller once, from the one
    # that DEFINES it. `collect_garbage` is bound in the bindings and
    # wrapped in the generated package under the same name, and two
    # imports of one name is the later one silently winning.
    seen: set[str] = set()
    for module in list(out):
        keep = [n for n in sorted(set(out[module])) if n not in seen]
        seen |= set(keep)
        if keep:
            out[module] = keep
        else:
            del out[module]
    return out


def module(bindings: list[str], generated: list[str],
           unions: list[str]) -> str:
    """The front door, as source.

    `X as X` on every import, unlike the bindings' front door. There
    is an `__all__` here too and it says the same thing - but this
    package is hand-written apart from this file, so a typechecker
    reads the real module rather than a stub, and the redundant
    spelling is what the hand-written version used. Keeping it means
    the emitted file is the file that was there, minus the
    maintenance.
    """
    by_module = exports(bindings, generated, unions)
    body: list[ast.stmt] = [ast.Expr(value=ast.Constant(value=DOC))]
    for name in LOCAL_MODULES:
        body.append(ast.ImportFrom(
            module=None, names=[ast.alias(name=name, asname=name)], level=1))
    for module_name, names in by_module.items():
        level = 1 if module_name.startswith(".") else 0
        body.append(ast.ImportFrom(
            module=module_name.lstrip("."),
            names=[ast.alias(name=n, asname=n) for n in names],
            level=level))
    everything = sorted({n for names in by_module.values() for n in names}
                        | set(LOCAL_MODULES))
    body.append(ast.Assign(
        targets=[ast.Name(id="__all__")],
        value=ast.List(elts=[ast.Constant(value=n) for n in everything])))
    out = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(out)
    return ast.unparse(out)


def emit(out_dir: str, manifest: Any) -> str:
    """Write `__init__.py` into the package directory, and say where.

    Takes the manifest rather than building one: the caller is a
    `setup.py`, which pays for the read once and would otherwise pay
    twice.
    """
    import pathlib

    from huggorm_decl import corpus
    from huggorm_gen.cppgen import pyinit
    from huggorm_gen.pygen import surface
    from huggorm_gen.pygen.emitter import package_exports

    have = corpus()
    bindings = [n for names in pyinit.exports(have).values() for n in names]
    ordered = surface.order(manifest)
    free = [name for name, proto in manifest["free_functions"].items()
            if proto["wrapped"]]
    generated = package_exports([p["name"] for p in ordered], free)
    unions = sorted(manifest["unions"])

    target = pathlib.Path(out_dir) / "__init__.py"
    target.write_text(module(bindings, generated, unions) + "\n")
    return str(target)
