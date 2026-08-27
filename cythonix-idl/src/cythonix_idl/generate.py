"""The build entry point: declarations in, C++ out.

`nbemit.py` is the emitter. This is the one file the BUILD runs, and
it exists so the build has a single command with a single answer to
"which declarations, and where do their files go".

    python3 -m cythonix_idl.generate <out-dir>

Nothing here decides anything. The two lists below are the whole
configuration, and each entry is a declaration that owns a module in
`cythonix_bindings`. A declaration not listed emits nothing, and a
module listed has no hand-written source at all.

That is the state the spike was arguing for. There is no `.pyx`, no
`.pxd` and no shim header anywhere in `cythonix_bindings`: every
module is C++ written from a declaration, and `declared_entries`,
`declared_functions` and `declared_returned` are how the generated
layer above learns what is in them - without importing a compiled
extension or parsing a pxd.
"""
import argparse
import ast
import pathlib
import sys

from cythonix_idl import manifest, nbemit, pyenum
from cythonix_idl.nbemit import bindable, extension
from cythonix_idl.read import read

HERE = pathlib.Path(__file__).resolve().parent

# Declarations that own a WHOLE module, through nanobind. Each emits
# one C++ translation unit and there is no hand-written source for it
# at all - no pyx, no pxd, no shim header.
#
# The `.pyx` route these took is gone. Every workaround it needed
# went with it: a store path crossed as its base name and was parsed
# back, absence was the empty string, a set became a vector of
# strings, and a bound object came back as an owning raw pointer.
# nanobind casts all four, so the declaration stopped carrying them.
NANOBIND = (
    "decl/path.py",
    "decl/pathinfo.py",
    "decl/store.py",
    "decl/eval.py",
    "decl/mock_store.py",
)

# Vocabularies. A StrEnum whose members ARE the strings a Nix parser
# takes, so there is no C++ and nothing to compile - the emitted
# module is plain Python and the build writes it whole, the way it
# writes a NANOBIND entry.
VOCABULARIES = (
    "decl/content_address.py",
)


# Which package the emitted bindings land in. The one fact a
# declaration does not carry: where a binding is installed is the
# build's decision.
PACKAGE = "cythonix_bindings"


def nanobind_modules() -> tuple[str, ...]:
    """The module name of each declaration compiled through nanobind.

    The build loops over these to know what to emit and what to
    compile, and `setup.py` reads the same list. One place names
    them, so a module cannot be emitted and then not compiled."""
    return tuple(pathlib.Path(name).stem for name in NANOBIND)


def declared_entries() -> dict[str, dict]:
    """Every declared class, as the manifest entry it implies.

    What `codegen` calls instead of reflecting. It used to build its
    manifest by parsing the pxd files and importing the compiled
    extension, which put every surface above it behind a C++
    compiler. Calling this ended that, one class at a time.

    A function call, not a file. An earlier version wrote JSON and
    handed the path over, which bought nothing: the specification is
    Python and so is its reader, so a serialisation in between is one
    more shape to keep in step and one more place a field can go
    missing quietly.

    Only classes are here. Enums, errors and free functions still
    come from reflection, so this is a seam that widens rather than a
    switch that flips.

    And only classes the emitter has FINISHED. A declaration under way
    describes a class it does not yet cover - `decl/store.py` carries
    four of nix::Store's eighteen methods today - and an entry built
    from half a declaration is not a smaller answer, it is a wrong
    one. The test is structural rather than a list to keep in step: a
    MODULES declaration owns a whole module and is complete by
    construction, and an INCLUDES declaration is complete for the
    values it emits, which is what `is_value` already says."""
    out = {}
    for name in NANOBIND:
        mod = read(str(HERE / name))
        known = mod.known
        for cls in mod.classes:
            entry = manifest.entry(cls, PACKAGE, mod.name, final=False,
                                   functions=mod.functions)
            # A SUBCLASS carries its base's methods, because that is
            # what deriving means on both sides of the binding: C++
            # inherits them and so does the Python class nanobind
            # builds. The declaration states each leaf's own policy
            # and its own additions, and says the rest once.
            #
            # Reflection got this for free - it read a live class,
            # where the methods are already there - and the layer
            # above needs it: an abstract base guarantees the
            # INTERSECTION of what its subclasses expose, so a leaf
            # that listed nothing would empty the base.
            base = known.get(cls.decl.base)
            if base is not None:
                mine = {m["name"] for m in entry["methods"]}
                inherited = manifest.entry(base, PACKAGE, base.module,
                                           final=False)["methods"]
                entry["methods"] = [m for m in inherited
                                    if m["name"] not in mine] + entry["methods"]
            out[cls.name] = entry
    return out


def declared_functions() -> dict[str, dict]:
    """Every free function a nanobind module offers, by name.

    The companion to `declared_entries`, and needed for the same
    reason one step further along: `model.extract_function` reads a
    live function's `inspect.signature`, and a nanobind function is a
    builtin with none.

    Only what the module actually offers. A startup hook and an
    exception translator are declared beside these because that is
    where a module's C++ facts live, and neither is surface; nor is a
    factory some class names, which is bound as that class's __new__
    instead. `nbemit.public` is where that last rule lives, and this
    reads it rather than repeating it."""
    out = {}
    for name in NANOBIND:
        mod = read(str(HERE / name))
        for fn in nbemit.public(mod.exported, mod.classes):
            out[fn.name] = manifest.function_entry(fn, PACKAGE, mod.name)
    return out


def declared_returned() -> list[str]:
    """Declared classes that are HANDED BACK, by name.

    A class a caller constructs is an entry point; a class that only
    ever arrives as somebody's return value is a returned type, and
    the generated layer treats the two differently - a returned type
    gets an `(obj, runner)` constructor so it can be adopted onto the
    runner that produced it.

    The generator answered this by walking the pxd. There is no pxd
    for a nanobind module, and the declaration knows anyway: a class
    is returned when some declared method returns it.

    Every name in the return type, not the type itself. A method
    returning `list[StorePath]` hands back StorePaths as surely as one
    returning a single StorePath does.

    And only a class nothing CONSTRUCTS. `@produced(by=...)` with no
    `__init__` is the whole test: StorePath is handed back by half of
    Store's methods and a caller can still build one from a base name,
    so it is an entry point that happens to be returned. Value cannot
    be built at all, and neither can PathInfo."""
    out: set[str] = set()
    for name in NANOBIND:
        mod = read(str(HERE / name))
        known = mod.known
        for cls in mod.classes:
            for m in cls.methods:
                if m.ret is None:
                    continue
                spelled = m.ret.python.removesuffix("| None").strip()
                if spelled.startswith("list["):
                    spelled = spelled[len("list["):-1]
                if spelled not in known or known[spelled].is_words:
                    continue
                held = known[spelled]
                if held.decl.built_by and held.ctor is None:
                    out.add(spelled)
    return sorted(out)


def emit_module(decl: str, dotted: str, out: str) -> int:
    """One declaration file, as the one C++ translation unit it owns.

    A file, not a class: a nanobind extension is one translation unit,
    and a declaration file may declare several classes, so all of them
    land in the one unit.

    `dotted` is where the module goes. `path` on its own is the
    standalone shape a spike builds; `cythonix_bindings.path` is the
    shape inside a package, and it is also what lets a module import
    the sibling whose types it names."""
    mod = read(str(HERE / decl) if not pathlib.Path(decl).is_absolute()
               else decl)
    bound = bindable(mod)
    if not bound:
        print(f"{decl}: nothing to bind", file=sys.stderr)
        return 2
    pathlib.Path(out).write_text(extension(mod, dotted))
    names = ", ".join(c.name for c in bound)
    print(f"{decl} -> {out} (module {dotted}): {names}")
    # How much of each class the declaration derived, and how much a
    # person wrote. Printed on every build, because a hatch nobody
    # measures becomes the place the real code lives - and a number
    # in a build log is cheaper than a review that has to notice.
    for cls in bound:
        c = nbemit.census(cls)
        hatch = (f", {c['hatched']} hatched ({c['hatch_lines']} lines)"
                 if c["hatched"] else "")
        print(f"  {cls.name}: {c['derived']} derived{hatch}")
    # What was left out, and why. A declaration under way declares
    # more than the emitter can carry, and a count that only ever
    # goes up is the honest way to see how much is left.
    skipped = [c.name for c in mod.classes if c not in bound]
    if skipped:
        print(f"  not bound: {', '.join(skipped)}")
    return 0


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    for name in NANOBIND:
        mod = read(str(HERE / name))
        target = out / f"{mod.name}.cpp"
        emit_module(name, f"{PACKAGE}.{mod.name}", str(target))
    for name in VOCABULARIES:
        source = HERE / name
        mod = read(str(source))
        target = out / f"{mod.name}.py"
        tree = ast.parse(source.read_text())
        target.write_text(pyenum.module(mod, tree, mod.doc) + "\n")
        words = [c.name for c in mod.classes if c.is_words]
        print(f"{name} -> {target}: {', '.join(words)}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    a = ap.parse_args()
    raise SystemExit(main(a.out_dir))
