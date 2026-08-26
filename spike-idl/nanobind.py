"""
Declaration -> nanobind C++.

The second backend, and the reason the declaration exists. `emit.py`
reads a `read.Class` and writes three Cython files; this reads the
SAME `read.Class` and writes one C++ function. Neither is the other's
input, and the declaration never learns which one ran.

## Why a targeted emitter works where a general one does not

`cgen` is the only real "C++ AST from Python" library, and it models
DECLARATIONS: `Struct`, `FunctionDeclaration`, `Template`. Its
expressions are strings - `Statement(text: str)`. A nanobind module
body is almost entirely one expression, so cgen hands back structure
for the part we do not need and strings for the part we do.

The way out is not a better C++ library. It is a smaller target.
Across nanopynix's 6,600 hand-written lines, 231 bindings use three
shapes - `.def`, `.def_prop_ro`, `.def_ro` - and not one lambda
captures anything. That is a vocabulary small enough to give every
shape a named builder.

## It emits what is CORRECT, not what was written first

Reproducing a hand-written file is the test, not the goal. Where the
two disagree and this emitter is right, it says so and keeps its own
answer. Three such places so far, each verified rather than assumed:

**`nb::is_operator()` on every comparison.** Without it a failed
overload raises TypeError; with it, nanobind returns NotImplemented
(`src/nb_func.cpp:530`). So `sp == None` currently raises in
nanopynix, which breaks `in`, `==` against None, and any generic code
that compares values of mixed type. Verified against the built
module.

**Ordering, when the declaration asks for it.** `nix::StorePath`
defaults `operator<=>` upstream, so `sorted(paths)` should work.
Nothing in nanopynix binds `__lt__`, so today it cannot.

**A view returns by pointer, not through a lambda.** With
`<nanobind/stl/string_view.h>` the caster copies into a Python str
(`PyUnicode_FromStringAndSize`), which is the same copy a hand-written
`std::string(...)` makes - so the lambda buys nothing and hides the
method behind a closure.

## What it refuses

A shape it cannot derive stops with a reason. The escape hatch is
`@custom`, counted and printed, because a hatch nobody measures
becomes the place the real code lives.
"""

from read import Class, Method, Type

INDENT = "    "

# How a declared type is spelled in a C++ signature, and which caster
# has to be included for it to cross. Nothing here is guessed from a
# Python name: a type reaches this table only through an Annotated
# alias that already carries its C++ spelling.
CXX_PARAM = {
    "string": ("const std::string &", "string"),
    "string_view": ("std::string_view", "string_view"),
    "bint": ("bool", None),
}

# Comparison dunders and the C++ operator each one binds. Every entry
# is emitted with nb::is_operator(); see the module docstring.
# A produced value's optional field needs an EXPLICIT C++ return type,
# because a lambda with two return paths - the value and std::nullopt -
# cannot deduce one. Derived from the declared Python type, so the
# declaration says `str | None` once and both backends read it.
CXX_OPTIONAL = {
    "str | None": "std::optional<std::string>",
    "int | None": "std::optional<std::int64_t>",
}

COMPARISONS = (("__eq__", "==", "value"), ("__lt__", "<", "order"),
               ("__le__", "<=", "order"), ("__gt__", ">", "order"),
               ("__ge__", ">=", "order"))


def _self(cls: Class) -> str:
    """The lambda's parameter name for the bound object.

    Invisible to Python - it names a C++ lambda argument - so the rule
    only has to be consistent. Initials of the CamelCase class:
    StorePath is `sp`, ValidPathInfo is `vpi`."""
    return "".join(c for c in cls.name if c.isupper()).lower() or "self"


def _param(t: Type) -> tuple[str, str | None]:
    if t.cxx is None or t.cxx.spelling not in CXX_PARAM:
        raise TypeError(
            f"'{t.python}' has no C++ parameter spelling. A bound class "
            f"names types through an Annotated alias in declare.py.")
    return CXX_PARAM[t.cxx.spelling]


def includes(cls: Class) -> list[str]:
    """Exactly the headers this binding needs, and no others.

    Derived from the declared types rather than listed. A caster left
    out does not fail at compile time - nanobind fails the conversion
    at RUNTIME with a bare std::bad_cast out of module init, which is
    a bad way to learn about a missing include.

    Two readings, because the two shapes speak different vocabularies.
    A bound class names C++ types through Annotated aliases, so the
    caster comes from the C++ spelling. A PRODUCED value's accessors
    are plain Python, so it comes from the Python one."""
    casters: set[str] = set()

    def from_cxx(t: Type) -> None:
        if (c := _param(t)[1]):
            casters.add(c)

    def from_python(spelled: str) -> None:
        if spelled in CXX_OPTIONAL:
            casters.add("optional")
        if "str" in spelled:
            casters.add("string")

    if cls.decl.built_by:
        for m in cls.methods:
            if m.ret is not None:
                from_python(m.ret.python)
    else:
        for m in cls.methods:
            for _, t in m.params:
                from_cxx(t)
            if m.ret is not None:
                from_cxx(m.ret)
        if cls.ctor is not None:
            for _, t in cls.ctor.params:
                from_cxx(t)

    out = ["#include <nanobind/nanobind.h>"]
    out += [f"#include <nanobind/stl/{c}.h>" for c in sorted(casters)]
    if cls.decl.wire == "value" and cls.decl.text:
        # std::hash lives in <functional>, and the value hash uses it.
        out.append("#include <functional>")
    out.append(f'#include "{cls.decl.header}"')
    return out


def _method(cls: Class, m: Method) -> list[str]:
    """One `.def`, bound by POINTER wherever nanobind allows it.

    A method pointer costs no lambda and no closure, and it keeps the
    C++ name visible in the emitted line - so a reader can see which
    upstream function is bound. `m.cxx_name` is the whole of the
    mapping, and the declaration is the only place it lives.

    A view returns by pointer too, because the string_view caster
    copies. `includes()` is what makes that safe, and it derives the
    header from this same declaration rather than trusting a human to
    remember."""
    assert m.ret is not None
    spelled = m.cxx_name or m.name
    return [f'{INDENT * 2}.def("{m.name}", &{cls.decl.cxx}::{spelled})']


def _ctor(cls: Class) -> list[str]:
    """`nb::init<...>`, with the declared parameter named for Python.

    `"name"_a` is what makes the parameter usable as a keyword, so the
    declaration's parameter NAME reaches callers rather than being
    decoration."""
    if cls.ctor is None:
        return []
    types = ", ".join(_param(t)[0] for _, t in cls.ctor.params)
    args = "".join(f', "{n}"_a' for n, _ in cls.ctor.params)
    line = f"{INDENT * 2}.def(nb::init<{types}>(){args}"
    if not cls.ctor.doc:
        return [line + ")"]
    # One line, however the declaration wrapped it: a C++ string
    # literal has no continuation and gluing two is noise.
    doc = " ".join(cls.ctor.doc.split()).replace("\\", "\\\\").replace('"', r'\"')
    return [line + ",", f'{INDENT * 3}     "{doc}")']


def _value_semantics(cls: Class) -> list[str]:
    """What a wire value owes Python, in nanobind's spelling.

    The same answers `_value.py` gives the Cython side, from the same
    two declarations - so a value prints and compares as the thing it
    IS on either backend, and neither emitter had to be told twice.

    Every comparison carries `nb::is_operator()`. That is not a style
    choice: without it, comparing against an unrelated type raises
    TypeError where Python's protocol wants NotImplemented."""
    decl = cls.decl
    obj = _self(cls)
    ref = f"const {decl.cxx} &{obj}"
    out: list[str] = []

    if decl.text:
        render = f"std::string({obj}.{decl.text}())"
        out.append(f'{INDENT * 2}.def("__str__", []({ref}) '
                   f"{{ return {render}; }})")
        out += [f'{INDENT * 2}.def("__repr__", []({ref}) {{',
                f'{INDENT * 3}return "{cls.name}(\'" + {render} + "\')";',
                f"{INDENT * 2}}})"]

    facts = {"value": decl.compare == "cxx",
             "order": decl.compare == "cxx" and decl.order}
    for name, op, fact in COMPARISONS:
        if not facts[fact]:
            continue
        # The C++ comparison, not a Python one on the rendered text.
        # Upstream defaults these, so declaring them means the binding
        # follows if that ever stops being true.
        out += [f'{INDENT * 2}.def("{name}", [](const {decl.cxx} &a, '
                f"const {decl.cxx} &b)",
                f"{INDENT * 3} {{ return a {op} b; }}, nb::is_operator())"]

    if decl.wire == "value" and decl.text:
        # Consistent with __eq__ by construction: both read the
        # accessor the declaration named.
        out += [f'{INDENT * 2}.def("__hash__", []({ref}) {{',
                f"{INDENT * 3}return std::hash<std::string_view>{{}}"
                f"({obj}.{decl.text}());",
                f"{INDENT * 2}}})"]
    return out


def _accessor(cls: Class, m: Method) -> list[str]:
    """One accessor of a PRODUCED value, in the smallest form it fits.

    Three forms, and the declaration picks by saying what it knows:

    `@reads("storeDir")` is a plain data member, so `def_ro` binds it
    and nanobind writes the accessor - `def_ro` IS `def_prop_ro` with
    a generated lambda (nb_class.h:784), so this is strictly less code
    for the same result.

    `@cxx_body(...)` is an accessor nothing can derive, and it becomes
    a `def_prop_ro` lambda carrying that source. `nix::ValidPathInfo`
    renders a store path against its own store directory; that is real
    logic, not a binding, and pretending otherwise would put a
    template where a person's decision belongs.

    Anything else is refused rather than guessed."""
    obj = _self(cls)
    if m.reads:
        return [f'{INDENT * 2}.def_ro("{m.name}", &{cls.decl.cxx}::{m.reads})']
    if m.cxx_body:
        body = m.cxx_body.strip().splitlines()
        spelled = CXX_OPTIONAL.get(m.ret.python if m.ret else "", "")
        ret = f" -> {spelled}" if spelled else ""
        head = (f'{INDENT * 2}.def_prop_ro("{m.name}", '
                f"[](const {cls.decl.cxx} &{obj}){ret} {{")
        return [head, *[f"{INDENT * 4}{ln}".rstrip() for ln in body],
                f"{INDENT * 2}}})"]
    raise TypeError(
        f"{cls.name}.{m.name}: a produced value's accessor must say what it "
        f"reads. Use @reads(\"member\") for a data member, or @cxx_body(...) "
        f"when it is computed.")


def _produced(cls: Class) -> list[str]:
    """A value the C++ side builds and Python only reads.

    Unlike the Cython backend, nothing is flattened. Cython cannot
    easily hand back a C++ struct, so cythonix copies nine fields into
    Python slots; nanobind binds `nix::ValidPathInfo` itself and each
    accessor reads the live object. So `@produced` means "no
    constructor" here and "no C++ at all" there - the same declared
    fact, two honest readings."""
    out: list[str] = []
    for m in cls.methods:
        out += _accessor(cls, m)
    return out


def bind_function(cls: Class) -> str:
    """The whole `bind_<name>` function for one declared class.

    A function per class, because that is the seam nanopynix already
    has: `nanopynix_module.cpp` calls `nanopynix_bind_store(store)`
    and friends. Generated code drops in beside hand-written code, one
    class at a time, and NB_MODULE does not change."""
    decl = cls.decl
    if not decl.cxx:
        raise TypeError(
            f"{cls.name}: no C++ type to bind. @binding(cxx=...) names it.")
    lines = [f"static void bind_{cls.name.lower()}(nb::module_ &m) {{",
             f'{INDENT}nb::class_<{decl.cxx}>(m, "{cls.name}")']
    if decl.built_by:
        # No nb::init: something else builds one, and offering a
        # constructor would advertise a way in that does not exist.
        body = _produced(cls)
    else:
        body = _ctor(cls)
        for m in cls.methods:
            body += _method(cls, m)
    body += _value_semantics(cls)
    for source in decl.custom.values():
        body += [f"{INDENT * 2}{line}".rstrip()
                 for line in source.splitlines()]
    if body:
        body[-1] += ";"
    return "\n".join([*lines, *body, "}"]) + "\n"


def module(cls: Class) -> str:
    """One translation unit: the includes, then the bind function."""
    head = [*includes(cls), "", "namespace nb = nanobind;",
            "using namespace nb::literals;", ""]
    return "\n".join(head) + "\n" + bind_function(cls)


def census(cls: Class) -> dict[str, int]:
    """How much of this class the declaration derived, and how much a
    person wrote.

    Printed on every run, like `emit.py`'s custom-hatch count. The
    ratio is the honest measure of a binding: StorePath derives whole
    and hatches nothing; ValidPathInfo joins a store directory to a
    path, which is a decision rather than a binding, and it says so
    with seven bodies."""
    derived = hatched = hatch_lines = 0
    for m in cls.methods:
        if m.cxx_body:
            hatched += 1
            hatch_lines += len(m.cxx_body.strip().splitlines())
        else:
            derived += 1
    if cls.ctor is not None:
        derived += 1
    derived += len(_value_semantics(cls)) and sum(
        1 for name, _, fact in COMPARISONS
        if {"value": cls.decl.compare == "cxx",
            "order": cls.decl.compare == "cxx" and cls.decl.order}[fact])
    return {"derived": derived, "hatched": hatched,
            "hatch_lines": hatch_lines}


if __name__ == "__main__":
    import sys

    from read import read

    for path in sys.argv[1:]:
        mod = read(path)
        for cls in mod.classes:
            print(module(cls) if len(mod.classes) == 1 else bind_function(cls))
            c = census(cls)
            total = c["derived"] + c["hatched"]
            print(f"// {cls.name}: {c['derived']}/{total} derived, "
                  f"{c['hatched']} through the hatch "
                  f"({c['hatch_lines']} lines of C++)", file=sys.stderr)
