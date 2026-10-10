"""The emitted package and its stubs, against the compiled bindings.

Each check here imports `huggorm_generated` or `huggorm_bindings`. The
checks that need neither are the generator's own suite:
`packages/huggorm-gen/tests/test_emitted.py`.
"""

import ast
import builtins
import importlib
import pathlib
import re
import sys

import pytest

import huggorm_generated
from huggorm_dsl.declare import Crossing
from huggorm_dsl.read import is_surface


@pytest.fixture
def out() -> pathlib.Path:
    """The installed package. Its stub package sits beside it."""
    return pathlib.Path(huggorm_generated.__file__).parent


def test_runtime_contract(out: pathlib.Path) -> None:
    """The emitter-runtime import contract. Generated modules reference
    the runtime only via `from _runtime import X`; a rename on either
    side otherwise ships a wheel that fails at first wrapper import.
    Every referenced symbol must exist, and the core trio must still be
    exercised at all."""
    import huggorm_generated._runtime as rt
    referenced = set()
    for py in sorted(out.glob("*.py")):
        tree = ast.parse(py.read_text(), filename=str(py.name))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "_runtime":
                referenced |= {a.name for a in node.names}
    missing = sorted(n for n in referenced if not hasattr(rt, n))
    assert not missing, f"emitted modules import missing _runtime symbols: {missing}"
    assert {"BaseRunner", "unwrap_arg"} <= referenced, (
        f"emitters stopped importing the core runtime: {sorted(referenced)}"
    )


def test_wrapping_and_wire_policy_are_separate_axes() -> None:
    """Immutable types are wire-values, everything else proxies. A class
    is wrapped when it needs a home thread (affine) or its methods can
    block. StorePath and PathInfo are pool and declare
    `_blocking = False`, so they cross every layer as themselves - no
    await in front of a substring read (huggorm#25)."""
    import huggorm_generated as flg
    from huggorm_gen.cppgen.generate import declared_model

    cls = declared_model().classes
    assert cls["StorePath"].wire is Crossing.VALUE
    assert cls["Value"].wire is Crossing.PROXY
    assert cls["PathInfo"].wire is Crossing.VALUE
    assert cls["EvalState"].wire is Crossing.PROXY
    for name in ("StorePath", "PathInfo"):
        assert not cls[name].blocking and not cls[name].wrapped, name
        assert not hasattr(flg, f"Async{name}"), f"Async{name} must not be generated"
        # With no handle to address, an rpc on it could never be called.
        assert not cls[name].served, name
    # The control: an affine class and a blocking pool class both stay
    # wrapped, so the rule is doing work rather than switching nothing.
    assert cls["Value"].wrapped is True
    assert cls["Store"].wrapped is True


def test_every_call_is_numbered_once() -> None:
    """A call crosses as its index into CALLS. The by-name tables must
    hold the same objects, or a client numbers a call the server reads
    as another."""
    from huggorm_generated._callspec import Acquire, Call
    from huggorm_generated._policy import ACQUIRE, CALLS, FREE, METHODS

    assert [c.index for c in CALLS] == list(range(len(CALLS)))
    named: list[Call | Acquire] = [*ACQUIRE.values(), *FREE.values(),
             *(m for ms in METHODS.values() for m in ms)]
    assert sorted(c.index for c in named) == list(range(len(CALLS)))
    for c in named:
        assert CALLS[c.index] is c, (c.index, c)


def test_stubs(out: pathlib.Path) -> None:
    """The stub package must describe the bindings EXACTLY.

    A stub package is authoritative: once huggorm_bindings-stubs exists, a
    typechecker stops looking at the real module, so a name the stubs
    omit becomes an error at every call site and a name they invent
    becomes a call that fails at runtime. Both directions matter, which
    is why this compares sets rather than checking coverage one way.

    Reflected against the LIVE modules, not the model. The model
    holds the generated surface, which the affine-return drop and 018's
    hierarchy split have already filtered; the bindings have neither."""
    import importlib

    from huggorm_gen.pygen.emitter import STUB_PACKAGE

    stub_dir = out.parent / STUB_PACKAGE
    assert stub_dir.is_dir(), f"no stub package at {stub_dir}"

    bindings = importlib.import_module("huggorm_bindings")
    failures = []
    checked = 0
    # Every stub's classes, because a base may be declared in another
    # module: `LayeredStore(Store)` (huggorm#149). Names are unique.
    everywhere: dict[str, ast.ClassDef] = {
        node.name: node
        for pyi in stub_dir.glob("*.pyi")
        for node in ast.parse(pyi.read_text()).body
        if isinstance(node, ast.ClassDef)}
    for pyi in sorted(stub_dir.glob("*.pyi")):
        if pyi.name == "__init__.pyi":
            continue
        module = importlib.import_module(f"{bindings.__name__}.{pyi.stem}")
        tree = ast.parse(pyi.read_text(), filename=pyi.name)

        declared, classes = set(), {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                declared.add(node.name)
                classes[node.name] = node
            elif isinstance(node, ast.FunctionDef):
                declared.add(node.name)
        live = {n for n in dir(module) if not n.startswith("_")
                and getattr(getattr(module, n), "__module__", None) == module.__name__}
        if declared != live:
            failures.append(
                f"{pyi.name}: declares {sorted(declared - live)} that do not "
                f"exist, and omits {sorted(live - declared)}")

        # Every name an annotation uses must be one the stub binds. A
        # stub that names a union alias the module never defines -
        # which every union return did - reads to a typechecker as an
        # unknown type, and nothing else here notices.
        bound = set(declared) | set(dir(builtins))
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                bound |= {(a.asname or a.name).split(".")[0] for a in node.names}
        used = set()
        for walked in ast.walk(tree):
            annotations = []
            if isinstance(walked, (ast.FunctionDef, ast.AsyncFunctionDef)):
                annotations += [a.annotation for a in (
                    *walked.args.posonlyargs, *walked.args.args,
                    *walked.args.kwonlyargs) if a.annotation is not None]
                if walked.returns is not None:
                    annotations.append(walked.returns)
            elif isinstance(walked, ast.AnnAssign):
                annotations.append(walked.annotation)
            for ann in annotations:
                used |= {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)}
        if used - bound:
            failures.append(
                f"{pyi.name}: annotations name {sorted(used - bound)}, which "
                f"the stub neither defines nor imports")

        # Everything the stub says a class has, including what it
        # inherits through a base the stub also declares. Defined once
        # over `classes` rather than rebuilt inside the loop: a closure
        # written in a loop body reads as a deferred capture of the
        # loop variable, which is a bug in every case but this one.
        def surface(n: str, classes: dict[str, ast.ClassDef] = everywhere) -> set[str]:
            out_: set[str] = set()
            cn = classes.get(n)
            if cn is None:
                return out_
            for b in cn.bases:
                if isinstance(b, ast.Name):
                    out_ |= surface(b.id)
            return out_ | {f.name for f in cn.body
                           if isinstance(f, ast.FunctionDef)
                           and is_surface(f.name)}

        for name in classes:
            cls = getattr(module, name)
            stub_methods = surface(name)
            live_methods = set()
            for k in cls.__mro__:
                if getattr(k, "__module__", "").split(".")[0] != bindings.__name__:
                    continue
                live_methods |= {a for a, v in k.__dict__.items()
                                 if is_surface(a)
                                 and (callable(v) or hasattr(v, "__get__"))}
            if stub_methods != live_methods:
                failures.append(
                    f"{pyi.name}:{name} declares "
                    f"{sorted(stub_methods - live_methods)} that do not "
                    f"exist, and omits {sorted(live_methods - stub_methods)}")
            checked += 1

    assert not failures, "stubs disagree with the bindings:\n  " + "\n  ".join(failures)
    assert checked >= 5, f"stub gate checked only {checked} class(es)"

    # The __init__ stub must re-export exactly what the real one does.
    init = ast.parse((stub_dir / "__init__.pyi").read_text())
    exported = {
        a.name
        for node in init.body if isinstance(node, ast.ImportFrom)
        for a in node.names
    }
    assert exported == set(bindings.__all__), (
        f"__init__.pyi exports {sorted(exported)}, the package exports "
        f"{sorted(bindings.__all__)}")
    # A plain import in a stub is PRIVATE; only `X as X` re-exports.
    aliased = {
        a.name
        for node in init.body if isinstance(node, ast.ImportFrom)
        for a in node.names if a.asname == a.name
    }
    assert aliased == exported, (
        f"these are imported but not re-exported: {sorted(exported - aliased)}")


def test_docstrings() -> None:
    """Every emitted module and class must carry a real __doc__. A
    string literal is only a docstring when nothing precedes it, so an
    import or a class attribute emitted first silently demotes it to a
    dead expression - the generated surface then documents itself to
    nobody, help() included."""
    import huggorm_generated as flg

    missing = []
    if not (flg.__doc__ or "").strip():
        missing.append("huggorm_generated")
    for name in flg.__all__:
        cls = getattr(flg, name)
        if not isinstance(cls, type) and not callable(cls):
            continue  # a re-exported registry, not a documented object
        mod = sys.modules[cls.__module__]
        if not (mod.__doc__ or "").strip():
            missing.append(f"module {cls.__module__}")
        if not (cls.__doc__ or "").strip():
            missing.append(f"class {name}")
    assert not missing, f"emitted objects without a docstring: {sorted(set(missing))}"


def test_annotations_resolve() -> None:
    """PEP 649 defers annotation evaluation, so a missing import only
    explodes when something calls typing.get_type_hints - which every
    introspecting consumer does, and every Python below 3.14 does at
    class creation time. Force resolution over the whole surface."""
    import typing

    import huggorm_generated as flg

    failures = []
    for name in flg.__all__:
        cls = getattr(flg, name)
        if not isinstance(cls, type) and not callable(cls):
            continue  # a re-exported registry, not an annotated object
        targets = [(name, cls)]
        for attr, val in vars(cls).items():
            fn = val.__func__ if isinstance(val, (staticmethod, classmethod)) else val
            if callable(fn):
                targets.append((f"{name}.{attr}", fn))
        for label, obj in targets:
            try:
                typing.get_type_hints(obj)
            except Exception as e:
                failures.append(f"{label}: {type(e).__name__}: {e}")
    assert not failures, "unresolvable annotations:\n" + "\n".join(failures)


def _rendered(sig: str) -> str:
    """One nanobind signature, as (types) -> type.

    Parameter NAMES are dropped and so are defaults. nanobind renders
    a default as an opaque `\\0` placeholder, and the names are the
    declaration's own - `"name"_a` comes straight off it, so comparing
    them would compare the declaration with itself."""
    body = sig[sig.index("(") + 1:sig.rindex(")")]
    ret = sig[sig.rindex(")") + 1:].removeprefix(" -> ")
    parts, depth, cur = [], 0, ""
    for ch in body:
        if ch in "[(":
            depth += 1
        elif ch in "])":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += ch
    parts.append(cur)
    typed = [p.split(":", 1)[1].split("=")[0] if ":" in p else p
             for p in (q.strip() for q in parts)
             if p and p.strip() != "self"]
    return f"({', '.join(_same(x) for x in typed)}) -> {_same(ret)}"


def _same(spelling: str) -> str:
    """One type, in the spelling both sides can be compared in.

    Three differences are real and none of them is a disagreement:

    - nanobind qualifies a bound class by its module, because that is
      what a caller must import. `StorePath` and
      `huggorm_bindings.path.StorePath` are one type. `pathlib.Path`
      keeps its module, because it is not one of ours.
    - a caster takes a `Sequence` and answers a `list`, and takes a
      `Mapping` and answers a `dict`. The declaration says `list` and
      `dict` for both, which is the surface a caller sees.
    - a VOCABULARY is a StrEnum whose members ARE the strings
      libstore parses, so it crosses as `str` and nanobind says so.
      That is the whole point of declaring it as words rather than
      binding it, and `_VOCABULARIES` is read from the model
      rather than listed here.
    - a UNION is an alias, and nanobind renders the arms it actually
      binds. `SingleDerivedPath` is `StorePath |
      SingleDerivedPathBuilt` by declaration, so the alias expands to
      exactly that and the two sides meet. Expanded REPEATEDLY,
      because an arm may itself name one - `_UNIONS` is read from the
      model, so nothing here lists an alias by hand.
    - an EXCEPTION a value holds crosses as `nb::object`, so nanobind
      says `object` and can say nothing else: a Python exception is
      not a bound C++ type and has no signature to render. The `|
      None` goes with it, because `nb::none()` IS the absent value
      there and the emitter writes no `std::optional` around it.
      `_ERRORS` is read from the model, like the two above.

    A DURATION needed none of this, which was measured rather than
    assumed. nanobind's chrono caster reads `datetime.timedelta |
    float` and writes `datetime.timedelta`, so a PARAMETER of one
    would disagree with the declaration - and no gate here compares
    one, because the only duration parameters are `_from_parts`'s and
    an `_`-prefixed name reaches no stub. A collapse of the input
    spelling was written here, passed, and was removed when taking it
    out changed nothing (huggorm#71)."""
    from huggorm_gen.payload.wiretypes import python_spelling

    out = re.sub(r"huggorm_bindings\.\w+\.", "", spelling)
    out = out.replace("collections.abc.Sequence[", "list[")
    out = out.replace("collections.abc.Mapping[", "dict[")
    for name in _ERRORS:
        out = re.sub(rf"\b{re.escape(name)}\b(\s*\|\s*None)?", "object", out)
    for name in _VOCABULARIES:
        out = re.sub(rf"\b{re.escape(name)}\b", "str", out)
    for _ in range(len(_UNIONS) + 1):
        before = out
        for alias, arms in _UNIONS.items():
            out = re.sub(rf"\b{re.escape(alias)}\b",
                         " | ".join(python_spelling(a) for a in arms), out)
        if out == before:
            break
    return re.sub(r"\s+", " ", out).strip()


_VOCABULARIES: set[str] = set()


# {alias: [arm, ...]}, from the model. See `_same`.
_UNIONS: dict[str, list[str]] = {}


# Declared EXCEPTION classes, from the model. See `_same`.
_ERRORS: set[str] = set()


def test_a_declared_type_is_the_type_nanobind_BINDS(
        out: pathlib.Path) -> None:
    """The declaration and the real C++ must agree, per method.

    The gate above catches a type nanobind cannot cast at all. This
    catches the other half: a declaration that says one thing while
    the C++ says another, where a caster happens to exist so nothing
    complains.

    The two gates split the space cleanly, and it is worth writing
    down which half each one holds, because it is not obvious.

    Caster includes are per TRANSLATION UNIT: `includes()` derives
    them from every declared type in the file, so ONE method declaring
    `StrView` pulls <nanobind/stl/string_view.h> in for every method
    beside it. A sibling that says `Str` where the C++ takes
    `std::string_view` then renders on the back of it - and it renders
    CORRECTLY, as `str`, because a caster exists and both spellings
    are `str` to Python. Measured: change one of the two and this gate
    stays green, rightly. Nothing a caller sees is wrong. Change both
    and no caster is included at all, which is the `::` gate's half.

    What this half holds is a declared PYTHON type that differs from
    the real one, and it is not cosmetic, because every surface above
    believes it. `Value.size` answers an `int` and
    was declared `Bint`: the stub said bool, the message carried
    `bool result = 1`, and a count of three crossed the wire as True.
    The C++ compiled, because nanobind casts an int to a Python bool
    without complaint.

    nanobind renders each signature from the C++ it actually calls, so
    it is the honest side of this comparison. Where the two disagree,
    the declaration is the one to fix."""
    from huggorm_gen.cppgen.generate import declared_model

    model = declared_model()
    _VOCABULARIES.clear()
    _VOCABULARIES.update(model.enums)
    _UNIONS.clear()
    _UNIONS.update({n: [a.name for a in u.arms]
                    for n, u in model.unions.items()})
    _ERRORS.clear()
    _ERRORS.update(model.errors.classes)

    bad, checked = [], 0
    for c in model.classes.values():
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        for meth in c.methods:
            fn = getattr(cls, meth.name, None)
            sigs = getattr(fn, "__nb_signature__", None)
            if not sigs:
                continue
            params = [_same(p.type.spelling)
                      # A parameter that reads None arrives as a
                      # std::optional so an explicit None works, and
                      # nanobind says so. The declaration says it with
                      # the default.
                      + (" | None" if p.defaults_to_none
                         and not p.type.optional else "")
                      for p in meth.params]
            want = f"({', '.join(params)}) -> {_same(meth.return_spelling)}"
            got = _rendered(sigs[0][0])
            checked += 1
            if got != want:
                bad.append(f"{c.name}.{meth.name}\n"
                           f"      nanobind: {got}\n"
                           f"      declared: {want}")
    assert not bad, (
        "the declaration disagrees with the C++ nanobind binds. nanobind "
        "reads the real signature, so the declaration is what to fix:\n    "
        + "\n    ".join(bad))
    assert checked, "no bound signature was found to check"


def test_no_binding_leaks_a_cxx_type(out: pathlib.Path) -> None:
    """Every bound signature must be spelled in PYTHON types.

    nanobind renders each binding's signature from the REAL C++ it
    calls, and it can only render a type it has a caster for. Where it
    has none, it prints the C++ spelling instead and refuses the call
    at run time:

        def parse_store_path(
            self, path: "std::basic_string_view<char, ...>") -> StorePath

    That is the failure this gate exists for, because nothing else
    catches it. The C++ compiles - the method pointer is valid. The
    declaration typechecks. The stub says `str`, the model says
    `str`, and a caller gets TypeError on the first call saying the
    supported argument type is a C++ type they have never heard of.

    It happened: `parseStorePath` and `followLinksToStorePath` take
    `std::string_view` (store-dir-config.hh:37, store-api.hh:370)
    while the declaration said `Str`. `includes()` derives the caster
    headers from the DECLARED types, so nothing pulled in
    <nanobind/stl/string_view.h> and both methods were unreachable.

    The check is one character, and that is the point: a `::` in a
    rendered signature is always a caster the emitter did not include.
    A test would catch it for a method that has one; this catches it
    for every method at once."""
    from huggorm_gen.cppgen.generate import declared_model

    bad, seen = [], 0
    for c in declared_model().classes.values():
        cls = getattr(importlib.import_module(c.qualified_module), c.name)
        for name in [m.name for m in c.methods] + ["__init__"]:
            fn = getattr(cls, name, None)
            for sig, *_ in getattr(fn, "__nb_signature__", None) or ():
                seen += 1
                if "::" in sig:
                    bad.append(f"{c.name}.{name}: {sig}")
    assert not bad, (
        "a bound signature names a C++ type, so nanobind has no caster "
        "for it and every call raises TypeError:\n  " + "\n  ".join(bad))
    assert seen, "no bound signature was found to check"
