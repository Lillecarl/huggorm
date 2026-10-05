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
module is C++ written from a declaration, and `declared_model` is how
the generated layer above learns what is in them - without importing
a compiled extension or parsing a pxd.
"""
import argparse
import ast
import functools
import pathlib
import re
import sys
from collections.abc import Sequence
from typing import Any

from huggorm_decl import CPP, corpus
from huggorm_dsl import declare
from huggorm_dsl.read import FROM_PARTS, Module
from huggorm_gen import ir
from huggorm_gen.cppgen import nbemit, pyenum, pyerrors, pyinit
from huggorm_gen.cppgen.nbemit import extension

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


@functools.cache
def declared_model() -> ir.Model:
    """The declaration set as the typed model, built once.

    Each module resolves its own names: `Module.known` is what that
    translation unit can name, its imports included."""
    have = corpus()
    classes: dict[str, ir.ClassModel] = {}
    functions: dict[str, ir.FunctionModel] = {}
    modules: list[ir.ModuleModel] = []
    seen: list[tuple[dict[str, Any], Any]] = []
    for mod in have.modules:
        unit = ir.ModuleModel.of(mod, PACKAGE)
        modules.append(unit)
        classes.update((c.name, c) for c in unit.classes)
        seen += [(mod.known, cls) for cls in mod.classes]
        functions.update((fn.name, fn) for fn in unit.exported)
    unions = {}
    for mod in have.modules:
        resolver = ir.Resolver.of(mod)
        for u in mod.unions:
            unions[u.name] = ir.UnionModel.of(u, resolver)
    vocabularies = tuple(
        ir.VocabularyModel(vocab.name, vocab.doc, tuple(
            ir.EnumModel.of(cls, PACKAGE, vocab.name)
            for cls in vocab.classes if cls.is_words))
        for vocab in map(have.module, have.vocabularies))
    enums = {e.name: e for vocab in vocabularies for e in vocab.enums}
    return ir.Model(classes, functions, unions, ir.returned_names(seen),
                    enums, _errors(), tuple(modules),
                    vocabularies)


def _errors() -> ir.Errors:
    """The exception surface. The module name is derived here for the
    same reason `errors_module` derives it: the emitter that writes the
    module decides where it goes."""
    have = corpus()
    if not have.errors:
        return ir.Errors("", {})
    mod = have.module(have.errors)
    return ir.Errors.of(errors_module(), mod.errors, ir.Resolver.of(mod))


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
    return pyerrors.chain(declared_model().errors, "huggorm::raise_as")


def error_headers() -> list[str]:
    """The headers the catch chain's types are declared in.

    Read from the same tree, at the same moment, as the chain itself.
    A catch and the include that makes its type nameable are one
    fact, and this is the emitter learning it rather than
    `cpp/errors.hpp` carrying it on the emitted files' behalf
    (huggorm#90)."""
    return declared_model().errors.headers


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


def census_written(mod: Module, bound: Sequence[ir.ClassModel],
                   text: str) -> None:
    """Every method the reader kept is a name the emitter wrote.

    The SECOND of the two seams huggorm#81 names. `census_read` asks
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
    listed, and `Module.exported` draws all three. A `@startup` hook
    is emitted as a CALL at module init and a `@translator` as a
    registration, so neither is a bound name. And a function marked
    `@constructs(cls)` is emitted as that class's constructor:
    `open_store` is `Store`'s `nb::new_`, and a caller writes
    `Store(uri)`.

    Only BOUND classes. `bindable` drops the ones nanobind cannot
    bind yet and `emit_module` prints what it left out, which is a
    different fact from an emitter losing one method of a class it
    did bind."""
    wrote = set(BOUND_NAME.findall(text))
    bad = []
    for cls in bound:
        for m in cls.bound:
            if m.name not in wrote:
                bad.append(f"{cls.name}.{m.name}()")
    for fn in mod.exported:
        if fn.name not in wrote:
            bad.append(f"{fn.name}()")
    if bad:
        raise TypeError(
            f"{mod.name}.py: the reader kept {', '.join(bad)} and the "
            f"emitted C++ binds no such name. An emitter dropped it, and "
            f"a drop reads as an absence - see huggorm#81.")


def emit_module(mod: Module, dotted: str, out: str,
                chain: list[str], headers: list[str] | None = None) -> int:
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
    unit = declared_model().module(mod.name)
    bound = unit.bindable()
    if not bound and not mod.functions:
        print(f"{decl}: nothing to bind", file=sys.stderr)
        return 2
    written = extension(unit, dotted, declared_model(),
                        chain=chain,
                        errors=errors_module(),
                        error_headers=headers or ())
    census_written(mod, bound, written)
    pathlib.Path(out).write_text(written)
    header = nbemit.records_header(unit, PACKAGE, declared_model())
    if header is not None:
        target = pathlib.Path(out).with_name(f"{mod.name}_records.hpp")
        target.write_text(header + "\n")
        print(f"{decl} -> {target}: records")
    names = ", ".join([c.name for c in bound] + [f.name for f in mod.functions])
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
    skipped = [c.name for c in unit.classes if c not in bound]
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
    the two agree and the gate says nothing. huggorm#75 is that bug:
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

    The gate huggorm#81 was opened for, at the first of the two seams
    it names. A declaration is read twice - parsed, and imported - and
    the reader keeps what BOTH readings agree on. When they stop
    agreeing it keeps less, and a definition that reaches nothing is
    indistinguishable from one nobody wrote.

    Three instances, none of them found by a check: a version-branched
    class the errors emitter never saw (huggorm#73), a `@property`
    accessor `_live` could not name (huggorm#75), and a whole file in
    none of the lists (huggorm#78, which the census beside this one
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
    """The same question for the errors declaration.

    Its emitted module is copied from the tree, methods and all. So
    the consumed side here is the RESOLVED tree - what `_resolve`
    kept - and the declared side is the raw parse, as above.

    Raw against RESOLVED, not against the emitted module. That is the
    seam: `resolved` is parse, then `_live`, then `_reconcile`, then
    `_resolve`, so a `_live` regression drops `to_dict` out of the
    emitted errors module exactly the way it dropped
    `registration_time` out of PathInfo (huggorm#75). Comparing the
    emitted module against the resolved tree would compare two things
    built from one read and agree with itself.

    `pyerrors` already refuses a tree it cannot resolve, which is the
    VERSION-BRANCH half (huggorm#73). This is the other half, and the
    two are different failures: one is a branch nobody chose, the
    other is a definition the reader lost.

    The CLASS grain is the one with teeth here, and the method grain
    is precautionary. Measured: `_resolve` appends a `ClassDef` whole
    and does not filter its body, so a method of a kept class cannot
    be dropped on this path at all - the per-method filtering happens
    in `_class`, which an exception never reaches. Perturbing `_live` to
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
    has carried it since the mock went (huggorm#60). The smoke test
    says so in a comment, where a person finds it only by reading the
    branch that never fires - so the build says it now.

    Read off the DECORATOR NODES rather than the reader's output. The
    reader keeps what a marker MEANT and throws away which word said
    it, and `@constructs(...)` and `@binds` both end up as fields
    that no longer name themselves.

    Printed rather than raised. A marker waiting for its user is a
    real state - `@abstract` is waiting for the split huggorm#61
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


# A member that HOLDS a Python object, as opposed to a parameter that
# passes one through. `nb::object fn) const;` is the wrapped tail of a
# function declaration and is not a member, so a line carrying a
# parenthesis is not one either.
_HOLDS = re.compile(r"nb::(object|callable|handle)\b")


def _python_members(text: str) -> list[str]:
    """Lines that declare a member holding a Python object.

    TEXT, not a parse. A C++ parser here would be a second compiler,
    and this only has to be right enough to ask a question - it
    reports, and a person answers.

    What it can miss is stated rather than hidden: a member reached
    through a typedef, a template parameter or a `std::function` that
    happens to wrap a callable. What it will not do is fire on a
    parameter, a call or a comment, which is what makes it quiet
    enough to be worth reading.
    """
    found = []
    for line in text.splitlines():
        bare = line.strip()
        if bare.startswith(("*", "//", "/*")):
            continue
        if "(" in bare or ")" in bare or not bare.endswith(";"):
            continue
        if _HOLDS.search(bare):
            found.append(bare)
    return found


def census_gc_slots(have: Any) -> None:
    """Every helper that stores a Python object, and whether anything
    declared over it says how to traverse one.

    huggorm#93 is why. A bound class that holds an `nb::object` and
    carries no `@gc_slots` leaks itself the moment that object closes
    over it - which is the NORMAL way to write the one case this repo
    has, because a primop builds its result with `state.make_int`.
    The leak is silent: the suite passes, and nanobind reports it at
    interpreter shutdown, after the last test.

    It is not an emitter skipping what it does not recognise. The
    emitter wrote exactly what the declaration said, and the
    declaration omitted something no rule required it to say. So the
    rule is here.

    PER FILE rather than per class, and that is a real limit. The
    `Evaluator` this exists for does not hold the callables itself -
    `EvalCore` does, and nothing binds `EvalCore` - so a per-class
    check would have to follow C++ members through a type nothing
    declares. Asking "does any declaration over this file say it" is
    the question a text scan can answer honestly.

    It WOULD have caught the original: `eval.hpp` held the callable
    and no declaration anywhere carried the marker.

    Printed, not raised, like `census_markers`. A helper storing a
    Python object may be perfectly safe for a reason only a person
    knows, and failing the build would get the marker added blindly
    rather than thought about."""
    slotted: dict[str, set[str]] = {}
    for name in have.module_names:
        for node in ast.walk(have.tree(name)):
            if not isinstance(node, ast.ClassDef):
                continue
            header, has_slots = "", False
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(
                        dec.func, ast.Name):
                    continue
                if dec.func.id == "header" and dec.args:
                    arg = dec.args[0]
                    if isinstance(arg, ast.Constant):
                        header = str(arg.value)
                elif dec.func.id == "gc_slots":
                    has_slots = True
            if header:
                slotted.setdefault(pathlib.Path(header).name, set())
                if has_slots:
                    slotted[pathlib.Path(header).name].add(node.name)
    for f in sorted(CPP.glob("*.hpp")):
        members = _python_members(f.read_text())
        if not members:
            continue
        says = slotted.get(f.name, set())
        if says:
            print(f"gc slots: {f.name} stores a Python object, and "
                  f"{', '.join(sorted(says))} says how to traverse it")
            continue
        print(f"  {f.name}: stores a Python object and NO declaration "
              f"over it carries @gc_slots - such a class leaks itself "
              f"whenever that object closes over it (huggorm#93)")
        for line in members:
            print(f"    {line}")


def main(out_dir: str) -> int:
    out = pathlib.Path(out_dir).resolve()
    # The package directory is EMPTY in the checkout - every file in
    # it is written here, `__init__.py` included (huggorm#64) - so an
    # empty directory is not something git can carry.
    out.mkdir(parents=True, exist_ok=True)
    have = corpus()
    # Read everything first, so a build reports every refusal it can
    # see rather than the first one. Emission below then runs against
    # a corpus known to be sound (huggorm#61).
    have.read_all()
    declared_model()
    chain = error_chain()
    headers = error_headers()
    for mod in have.modules:
        target = out / f"{mod.name}.cpp"
        emit_module(mod, f"{PACKAGE}.{mod.name}", str(target), chain, headers)
    tree = have.resolved(have.errors)
    doc = ast.get_docstring(tree, clean=False) or ""
    # Named after the declaration, not "errors.py". `errors_module()`
    # derives the import path from the same stem, so a hardcoded file
    # name here would let the two disagree - and renaming the
    # declaration proved they did.
    target = out / f"{pathlib.Path(have.errors).stem}.py"
    target.write_text(pyerrors.module(tree, doc, PACKAGE) + "\n")
    print(f"{have.errors} -> {target}")
    for vocab in declared_model().vocabularies:
        target = out / f"{vocab.name}.py"
        target.write_text(pyenum.module(vocab) + "\n")
        words = ", ".join(e.name for e in vocab.enums)
        print(f"{vocab.name}.py -> {target}: {words}")
    # LAST, because it re-exports what everything above emitted.
    # Nothing reads it during the build - setuptools does - so the
    # order is for a reader rather than for correctness.
    target = out / "__init__.py"
    target.write_text(pyinit.module(declared_model()) + "\n")
    names = sum(len(v) for v in declared_model().exports.values())
    print(f"front door -> {target}: {names} name(s)")
    census_cpp(set(have.module_names))
    census_markers(have)
    census_gc_slots(have)
    # LAST of the three, and the only one that raises. It reads the
    # RAW parse, so it is the one check no earlier stage can have
    # already agreed with (huggorm#81).
    census_read(have)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir")
    a = ap.parse_args()
    raise SystemExit(main(a.out_dir))
