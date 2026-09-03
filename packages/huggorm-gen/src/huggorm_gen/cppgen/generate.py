"""The build entry point: declarations in, C++ out.

`nbemit.py` is the emitter. This is the one file the BUILD runs, and
it exists so the build has a single command with a single answer to
"which declarations, and where do their files go".

    python3 -m huggorm_gen.cppgen.generate <out-dir>

Nothing here decides anything. The two lists below are the whole
configuration, and each entry is a declaration that owns a module in
`huggorm_bindings`. A declaration not listed emits nothing, and a
module listed has no hand-written source at all.

That is the state the spike was arguing for. There is no `.pyx`, no
`.pxd` and no shim header anywhere in `huggorm_bindings`: every
module is C++ written from a declaration, and `declared_entries`,
`declared_functions` and `declared_returned` are how the generated
layer above learns what is in them - without importing a compiled
extension or parsing a pxd.
"""
import argparse
import ast
import pathlib
import re
import sys
from typing import Any

from huggorm_decl import CPP, corpus
from huggorm_dsl import declare
from huggorm_dsl.read import FROM_PARTS, Module, reading
from huggorm_gen.cppgen import manifest, nbemit, pyenum, pyerrors, pyinit
from huggorm_gen.cppgen.nbemit import bindable, extension

# Which package the emitted bindings land in. The one fact a
# declaration does not carry: where a binding is installed is the
# build's decision.
PACKAGE = "huggorm_bindings"


def nanobind_modules() -> tuple[str, ...]:
    """The module name of each declaration compiled through nanobind.

    The build loops over these to know what to emit and what to
    compile, and `setup.py` reads the same list. One place names
    them, so a module cannot be emitted and then not compiled."""
    return corpus().module_names


def declared_entries() -> dict[str, dict[str, Any]]:
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
    for mod in corpus().modules:
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


def declared_unions() -> dict[str, list[str]]:
    """Every declared SUM type, as {alias: [arm, ...]}.

    The companion to `declared_entries`, and it exists for the same
    reason one step sideways: a union is not a class, so nothing
    downstream can reflect one off the compiled package. The alias is
    module-level Python - `DerivedPath = StorePath | DerivedPathBuilt`
    - and it never reaches an extension at all.

    Arms in DECLARED order, because that is the order the schema
    numbers a oneof's fields in and a renumbering is a wire change."""
    out: dict[str, list[str]] = {}
    for mod in corpus().modules:
        for union in mod.unions:
            out[union.name] = list(union.decl.arms)
    return out


def declared_functions() -> dict[str, dict[str, Any]]:
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
    for mod in corpus().modules:
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
    for mod in corpus().modules:
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


# The exception hierarchy, declared once. It emits two things that
# used to be written twice and had to agree: the Python module a
# caller catches, and the translator's catch chain.


def error_chain() -> list[str]:
    """The translator's catch chain, from the declaration.

    `raise_as` stays hand-written: turning a std::exception into a
    live Python exception is nanobind's protocol, not something a
    declaration knows. What is derived is WHICH classes, in WHAT
    ORDER, and WHERE to find them - the order is the part a person
    gets wrong, and the where is the part that was written twice.

    `errors_module()` is the same call that names the emitted module
    and fills `_policy.ERROR_MODULE`, so the catch chain cannot point
    somewhere the module is not."""
    have = corpus()
    return pyerrors.chain(have.resolved(have.errors), "huggorm::raise_as",
                          errors_module())


def declared_errors() -> dict[str, Any]:
    """The exception surface, as the manifest carries it.

    What `model.extract_errors` reflected. It imported the compiled
    bindings package to reach the emitted `errors.py`, which made a
    pure-Python fact - a class statement and its bases - wait on a
    C++ compiler.

    The module name is derived here for the same reason
    `errors_module` derives it: the emitter that writes the module
    decides where it goes."""
    have = corpus()
    if not have.errors:
        return {"module": None, "classes": {}}
    # Named, so a refusal from `entries` carries the file it is about
    # rather than `<declaration>`. The reader does this for itself;
    # an emitter reading a tree the corpus already parsed has to say
    # so (tasks/061).
    with reading(str(have.path(have.errors))):
        return {"module": errors_module(),
                "classes": pyerrors.entries(have.resolved(have.errors),
                                            have.imported(have.errors))}


def declared_enums() -> dict[str, dict[str, Any]]:
    """Every string vocabulary, as the manifest carries it.

    The companion to `declared_entries`, and the last group that came
    from reflection. A vocabulary was found by asking the compiled
    package for classes that subclass both str and Enum - true, and
    a whole C++ build to learn what `decl/words.py` says outright.

    Only a `@words` class. A vocabulary declaration holds nothing
    else, and `is_words` is the declaration's own word for it."""
    out: dict[str, dict[str, Any]] = {}
    have = corpus()
    for name in have.vocabularies:
        mod = have.module(name)
        for cls in mod.classes:
            if cls.is_words:
                out[cls.name] = manifest.words_entry(cls, PACKAGE, mod.name)
    return out


def declared_bases() -> dict[str, str]:
    """Each declared class to its declared base, by name.

    `pygen._hierarchy` walked `cls.__mro__` for this, which is the
    same question asked of a compiled object. A declaration states
    its base outright, and states one - every hierarchy this binds is
    single inheritance.

    Only a base the set DECLARES. A class deriving from something
    outside it has no emitted ancestor, which is what the MRO walk
    meant by "the nearest class that is also emitted"."""
    have = corpus()
    known = {c.name for c in have.classes}
    return {c.name: c.decl.base for c in have.classes
            if c.decl.base in known}


def errors_module() -> str:
    """Where the emitted exception module lands, as an import path.

    Derived, not declared. The bindings package used to carry
    `_errors_module = "huggorm_bindings.errors"` as a marker, and
    pygen read it off the imported package. Both halves of that
    string are already known here: `PACKAGE` is where a binding is
    installed, and the declaration set names the errors document.

    A marker is right when a fact has nowhere else to live. This one
    had somewhere, so it was a fact stated twice - and the two would
    have disagreed the first time either half moved."""
    have = corpus()
    return f"{PACKAGE}.{pathlib.Path(have.errors).stem}" if have.errors else ""


def _code_lines(text: str) -> int:
    """Lines of C++ that are not blank and not a comment.

    Prose is not the thing being counted. `cpp/eval.hpp` is 601 lines
    and 304 of them explain WHY, which is this repo's standard rather
    than its debt - counting them made the number look three times
    worse than it is and, more importantly, made it look unfixable."""
    n, inblock = 0, False
    for line in text.splitlines():
        t = line.strip()
        if not t:
            continue
        if t.startswith("/*"):
            inblock = True
        if inblock or t.startswith(("//", "*")):
            if "*/" in t:
                inblock = False
            continue
        n += 1
    return n


# What a bound name looks like in the emitted C++. nanobind spells a
# method `.def("name", ...)` and a free function `m.def("name", ...)`,
# and both end in `def("`.
BOUND_NAME = re.compile(r'def\("([^"]+)"')


def census_written(mod: Module, bound: tuple[Any, ...], text: str) -> None:
    """Every method the reader kept is a name the emitter wrote.

    The SECOND of the two seams `tasks/081` names. `census_read` asks
    whether the reader kept what the declaration wrote; this asks
    whether the emitter wrote what the reader kept.

    They need different instruments, and that is the point. A reader
    drop is invisible to anything built from the read, so that one
    compares against the raw parse. An emitter skip is invisible to
    the reader's output, so this one compares against the TEXT the
    emitter just produced - not against a second walk of the same
    `Class` objects, which would be two views of one decision
    agreeing with itself.

    Here rather than in a later pass because this is the one place
    both halves exist at once: `mod` is what the emitter was handed
    and `text` is what it wrote.

    Three exemptions, each read off the declaration rather than
    listed. A `@startup` hook is emitted as a CALL at module init and
    a `@translator` as a registration, so neither is a bound name -
    `Module.exported` already draws that line. And the function a
    class names in `@produced(by=...)` is emitted as that class's
    constructor: `open_store` is `Store`'s `nb::new_`, and a caller
    writes `Store(uri)`.

    Only BOUND classes. `bindable` drops the ones nanobind cannot
    bind yet and `emit_module` prints what it left out, which is a
    different fact from an emitter losing one method of a class it
    did bind."""
    wrote = set(BOUND_NAME.findall(text))
    factories = {c.decl.built_by for c in mod.classes if c.decl.built_by}
    bad = []
    for cls in bound:
        for m in cls.methods:
            if m.name not in wrote:
                bad.append(f"{cls.name}.{m.name}()")
    for fn in mod.exported:
        if fn.name not in wrote and fn.name not in factories:
            bad.append(f"{fn.name}()")
    if bad:
        raise TypeError(
            f"{mod.name}.py: the reader kept {', '.join(bad)} and the "
            f"emitted C++ binds no such name. An emitter dropped it, and "
            f"a drop reads as an absence - see tasks/081.")


def emit_module(mod: Module, dotted: str, out: str,
                chain: list[str]) -> int:
    """One declaration, as the one C++ translation unit it owns.

    A file, not a class: a nanobind extension is one translation unit,
    and a declaration file may declare several classes, so all of them
    land in the one unit.

    Takes the `Module` rather than a file name. It used to take the
    name and read the file itself, which read every declaration a
    second time - `main` had already read it to learn where the
    output goes.

    `chain` for the same reason. The catch chain is one fact about
    the whole set, and building it here meant parsing `errors.py`
    once per module.

    `dotted` is where the module goes. `path` on its own is the
    standalone shape a spike builds; `huggorm_bindings.path` is the
    shape inside a package, and it is also what lets a module import
    the sibling whose types it names."""
    decl = f"{mod.name}.py"
    bound = bindable(mod)
    if not bound:
        print(f"{decl}: nothing to bind", file=sys.stderr)
        return 2
    written = extension(mod, dotted, chain=chain, errors=errors_module())
    census_written(mod, bound, written)
    pathlib.Path(out).write_text(written)
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
    # ...and the C++ this repo WROTE for the module, which the
    # per-class census cannot see.
    #
    # A hatch nobody measures becomes the place the real code lives -
    # that is the bargain `cpp/README` makes - and `cpp/` was
    # exactly such a hatch: `census` counts `Cxx` bodies and `@custom`
    # blocks, both of which live in a declaration, while a helper a
    # declaration NAMES landed in a directory no number ever read.
    # Found by cython-reviewer, reviewing a change that added thirty
    # lines there.
    #
    # Found through huggorm_decl, which is where a helper lives now.
    # It used to be found beside the emitted `.cpp`, because the
    # helpers sat in the build's copy of `huggorm_bindings`; they sit
    # with the declarations that name them instead.
    helper = CPP / f"{mod.name}.hpp"
    hand = _code_lines(helper.read_text()) if helper.exists() else 0
    # BOTH numbers, and the second is why. A body moved out of cpp/
    # and into a `Cxx(...)` in the declaration is better - the reader
    # of the declaration sees the decision - but it is still C++ a
    # person wrote, and counting only the first would let the second
    # absorb it silently.
    bodies = sum(_code_lines(m.cxx_body) for c in mod.classes
                 for m in c.methods) + sum(
                     _code_lines(f.cxx_body) for f in mod.functions)
    if hand or bodies:
        print(f"  hand-written C++: {hand} in cpp/{helper.name}, "
              f"{bodies} in Cxx bodies")
    return 0


def _under_a_branch(node: ast.AST, target: ast.AST) -> bool:
    """Whether `target` sits inside an `if` within `node`.

    The one definition a declaration may write that reaches no output:
    a `NIX_VERSION` arm this build is not. Only one arm survives the
    import, so the other is MEANT to vanish.

    Structural, not a name. The alternative was to compare against the
    RESOLVED tree, which is what `_resolve` already produced - and
    that is exactly the blind spot this gate exists to cover. A reader
    that drops a node wrongly drops it from the resolved tree too, so
    the two agree and the gate says nothing. `tasks/075` is that bug:
    a `@property` accessor named no live line and vanished from every
    output in silence."""
    for parent in ast.walk(node):
        if not isinstance(parent, ast.If):
            continue
        for inner in (*parent.body, *parent.orelse):
            if any(n is target for n in ast.walk(inner)):
                return True
    return False


def census_read(have: Any) -> None:
    """Every definition a declaration writes reaches the reader.

    The gate `tasks/081` was opened for, at the first of the two seams
    it names. A declaration is read twice - parsed, and imported - and
    the reader keeps what BOTH readings agree on. When they stop
    agreeing it keeps less, and a definition that reaches nothing is
    indistinguishable from one nobody wrote.

    Three instances, none of them found by a check: a version-branched
    class the errors emitter never saw (`tasks/073`), a `@property`
    accessor `_live` could not name (`tasks/075`), and a whole file in
    none of the lists (`tasks/078`, which the census beside this one
    now catches at file grain).

    Read from the RAW parse, which is the whole design. Everything
    downstream reads the resolved tree or the `Class` objects built
    from it, so a reader that drops a node wrongly makes every one of
    them agree.

    A definition is CONSUMED when the reader put it somewhere: in
    `methods`, or as the `ctor`, or as `from_parts`. Those last two
    are the two shapes that are not methods and are not skipped -
    `__init__` becomes the constructor and `_from_parts` becomes the
    round-trip helper - and asking WHERE the reader put it derives the
    exemption instead of listing the two names.

    Raises rather than prints, unlike `census_markers` beside it. An
    unused marker is a real waiting state; a declaration nobody read
    is this session's bug class four times over."""
    bad = []
    for name in (*have.nanobind, *have.vocabularies):
        mod = have.module(name)
        seen = {c.name: c for c in mod.classes}
        functions = {f.name for f in mod.functions}
        raw = ast.parse(have.path(name).read_text())
        for node in raw.body:
            if isinstance(node, ast.FunctionDef):
                if node.name not in functions and not _under_a_branch(raw, node):
                    bad.append(f"{name}: {node.name}() is declared and the "
                               f"reader kept no function by that name")
                continue
            if not isinstance(node, ast.ClassDef):
                continue
            cls = seen.get(node.name)
            if cls is None:
                if not _under_a_branch(raw, node):
                    bad.append(f"{name}: class {node.name} is declared and "
                               f"the reader kept no class by that name")
                continue
            kept = {m.name for m in cls.methods}
            for at in (cls.ctor, cls.from_parts):
                if at is not None:
                    kept.add(at.name)
            for sub in node.body:
                if not isinstance(sub, ast.FunctionDef):
                    continue
                if sub.name in kept or _under_a_branch(node, sub):
                    continue
                bad.append(
                    f"{name}: {node.name}.{sub.name}() is declared and "
                    f"reaches nothing - the reader kept no method, no "
                    f"constructor and no {FROM_PARTS} by that name")
    bad += _errors_read(have)
    if bad:
        raise TypeError(
            "a declaration writes definitions nothing reads. " + " ".join(bad))


def _errors_read(have: Any) -> list[str]:
    """The same question for the errors declaration, which is not a Module.

    Nothing reads `errors.py` as a `Module`: it emits a Python module
    and a C++ catch chain, both written from the tree. So the consumed
    side here is the RESOLVED tree - what `_resolve` kept - and the
    declared side is the raw parse, as above.

    Raw against RESOLVED, not against the emitted module. That is the
    seam: `resolved` is parse, then `_live`, then `_reconcile`, then
    `_resolve`, so a `_live` regression drops `to_dict` out of the
    emitted errors module exactly the way it dropped
    `registration_time` out of PathInfo (`tasks/075`). Comparing the
    emitted module against the resolved tree would compare two things
    built from one read and agree with itself.

    `pyerrors` already refuses a tree it cannot resolve, which is the
    VERSION-BRANCH half (`tasks/073`). This is the other half, and the
    two are different failures: one is a branch nobody chose, the
    other is a definition the reader lost.

    The CLASS grain is the one with teeth here, and the method grain
    is precautionary. Measured: `_resolve` appends a `ClassDef` whole
    and does not filter its body, so a method of a kept class cannot
    be dropped on this path at all - the per-method filtering happens
    in `_class`, which errors.py never reaches. Perturbing `_live` to
    forget one class does fire this:

        errors.py: SysError is declared and the resolved tree has no
        such definition

    ...and that is an exception class gone from the emitted module and
    from the C++ catch chain, in silence. Perturbing it to forget a
    METHOD fires nothing, because nothing drops one."""
    if not have.errors:
        return []
    raw = ast.parse(have.path(have.errors).read_text())
    kept = have.resolved(have.errors)

    def names(tree: ast.Module) -> set[str]:
        out = set()
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            out.add(node.name)
            out |= {f"{node.name}.{s.name}" for s in node.body
                    if isinstance(s, ast.FunctionDef)}
        return out

    bad = []
    for node in raw.body:
        if not isinstance(node, ast.ClassDef):
            continue
        here: dict[str, ast.stmt] = {node.name: node}
        here.update({f"{node.name}.{s.name}": s for s in node.body
                     if isinstance(s, ast.FunctionDef)})
        for spelled, at in here.items():
            if spelled in names(kept) or _under_a_branch(raw, at):
                continue
            bad.append(f"{have.errors}: {spelled} is declared and the "
                       f"resolved tree has no such definition")
    return bad


def census_cpp(claimed: set[str]) -> None:
    """Every hand-written C++ file, counted, including the orphans.

    `emit_module` counts `cpp/<module>.hpp` for the module it is
    emitting. That leaves any helper whose name is not a module name
    uncounted, and two were: `errors.hpp` and `libstore.hpp`, 60 lines
    between them, invisible on every build since they were written.

    "A hatch nobody measures becomes the place the real code lives" is
    this repo's own rule, and the per-module census WAS such a hatch -
    it measured the files it happened to look up. This measures the
    directory.

    Named separately in the output rather than folded into a total. A
    file no module claims is the interesting case: it is C++ that no
    declaration is emitting beside, so nothing in a build log points
    at the declaration that should have absorbed it."""
    files = sorted(f for f in CPP.glob("*.hpp"))
    if not files:
        return
    total = sum(_code_lines(f.read_text()) for f in files)
    print(f"hand-written C++ in cpp/: {total} lines in {len(files)} file(s)")
    orphans = [f for f in files if f.stem not in claimed]
    for f in orphans:
        print(f"  {f.name}: {_code_lines(f.read_text())} lines, claimed by "
              f"no module - no declaration is emitted beside it")


def census_markers(have: Any) -> None:
    """Every declared marker, and which of them nothing uses.

    `census_cpp` counts hand-written C++ because an unmeasured hatch
    becomes the place the real code lives. This counts the other
    resource the same way: a marker nothing uses is an emitter branch
    nothing runs, and it is worse than dead code because it looks
    supported.

    `@abstract` is why this exists. It has a table entry, three
    emitter branches and a smoke-test assertion, and no declaration
    has carried it since the mock went (tasks/060). The smoke test
    says so in a comment, where a person finds it only by reading the
    branch that never fires - so the build says it now.

    Read off the DECORATOR NODES rather than the reader's output. The
    reader keeps what a marker MEANT and throws away which word said
    it, and `@produced(by=...)` and `@binds` both end up as fields
    that no longer name themselves.

    Printed rather than raised. A marker waiting for its user is a
    real state - `@abstract` is waiting for the split tasks/061
    describes - and failing the build would only get it deleted."""
    used: set[str] = set()
    names = [*have.module_names, *have.vocabularies]
    if have.errors:
        names.append(have.errors)
    for name in names:
        for node in ast.walk(have.tree(name)):
            for dec in getattr(node, "decorator_list", []):
                if isinstance(dec, ast.Call):
                    dec = dec.func
                if isinstance(dec, ast.Name):
                    used.add(dec.id)
    declared = set(declare.MARKERS)
    unused = sorted(declared - used)
    print(f"markers: {len(declared)} declared, "
          f"{len(declared) - len(unused)} used")
    for name in unused:
        print(f"  @{name}: no declaration carries it - the emitter "
              f"branches behind it have never run")


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    # The package directory is EMPTY in the checkout - every file in
    # it is written here, `__init__.py` included (tasks/064) - so an
    # empty directory is not something git can carry.
    out.mkdir(parents=True, exist_ok=True)
    have = corpus()
    # Read everything first, so a build reports every refusal it can
    # see rather than the first one. Emission below then runs against
    # a corpus known to be sound (tasks/061).
    have.read_all()
    chain = error_chain()
    for mod in have.modules:
        target = out / f"{mod.name}.cpp"
        emit_module(mod, f"{PACKAGE}.{mod.name}", str(target), chain)
    tree = have.resolved(have.errors)
    doc = ast.get_docstring(tree, clean=False) or ""
    # Named after the declaration, not "errors.py". `errors_module()`
    # derives the import path from the same stem, so a hardcoded file
    # name here would let the two disagree - and renaming the
    # declaration proved they did.
    target = out / f"{pathlib.Path(have.errors).stem}.py"
    target.write_text(pyerrors.module(tree, doc) + "\n")
    print(f"{have.errors} -> {target}")
    for name in have.vocabularies:
        mod = have.module(name)
        target = out / f"{mod.name}.py"
        target.write_text(
            pyenum.module(mod, have.tree(name), mod.doc) + "\n")
        words = [c.name for c in mod.classes if c.is_words]
        print(f"{name} -> {target}: {', '.join(words)}")
    # LAST, because it re-exports what everything above emitted.
    # Nothing reads it during the build - setuptools does - so the
    # order is for a reader rather than for correctness.
    target = out / "__init__.py"
    target.write_text(pyinit.module(have) + "\n")
    names = sum(len(v) for v in pyinit.exports(have).values())
    print(f"front door -> {target}: {names} name(s)")
    census_cpp(set(have.module_names))
    census_markers(have)
    # LAST of the three, and the only one that raises. It reads the
    # RAW parse, so it is the one check no earlier stage can have
    # already agreed with (tasks/081).
    census_read(have)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    a = ap.parse_args()
    raise SystemExit(main(a.out_dir))
