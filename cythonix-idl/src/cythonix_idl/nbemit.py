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

import ast
import json
from collections.abc import Sequence

from cythonix_idl.declare import Field
from cythonix_idl.read import Class, Method, Module, Type

INDENT = "    "

# How a declared type is spelled in a C++ signature, and which caster
# has to be included for it to cross. Nothing here is guessed from a
# Python name: a type reaches this table only through an Annotated
# alias that already carries its C++ spelling.
CXX_PARAM = {
    "string": ("const std::string &", "string"),
    "string_view": ("std::string_view", "string_view"),
    "bint": ("bool", None),
    "uint64_t": ("std::uint64_t", None),
    "int64_t": ("std::int64_t", None),
}

# A declared Python type with no C++ alias behind it, and the C++ it
# crosses as. Each entry is a caster nanobind ships, so nothing here
# is flattened on the way through.
#
# This is where the two backends part company. Cython could declare
# none of these - a pxd has no std::set, no std::optional and no
# non-default-constructible member - so its emitter turned every one
# of them into a string and parsed it back. nanobind casts them, so
# a store path stays a store path from libstore to Python.
CXX_PYTHON = {
    "bytes": ("nb::bytes", None),
    "pathlib.Path": ("const std::filesystem::path &", "filesystem"),
}

# The plain reading of a builtin, for a declaration that annotates
# with the builtin and no alias. A LAST resort, unlike CXX_PYTHON
# above: an alias is the declaration saying which C++ it means, and
# `str` alone says only that Python sees a str.
#
# The difference is what `StrView` and `Path` need. Both are `str`-ish
# to Python and both carry Cxx("string"), because a pxd can hold
# nothing else - so a table that let either side win outright would
# turn one of them into the wrong crossing.
CXX_BUILTIN = {
    "str": ("const std::string &", "string"),
    "bool": ("bool", None),
}

# Comparison dunders and the C++ operator each one binds. Every entry
# is emitted with nb::is_operator(); see the module docstring.
# A produced value's optional field needs an EXPLICIT C++ return type,
# because a lambda with two return paths - the value and std::nullopt -
# cannot deduce one. Derived from the declared Python type, so the
# declaration says `str | None` once and both backends read it.
# A Python default, spelled for C++. Only where the two differ: a
# number or a string literal already reads the same in both.
CXX_DEFAULT = {"True": "true", "False": "false", "None": "nullptr"}

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


# Where an emitted C++ type goes. The same namespace `errors.hpp`
# already opens, so a translation unit that includes it reopens one
# namespace rather than gaining a second.
NAMESPACE = "cythonix"


def _held(cls: Class) -> str:
    """The C++ type this class binds.

    Two sources, and which one applies is the difference between a
    HANDLE and a RECORD. `@binding(cxx=...)` names a type libstore
    already has, and the binding holds one of those. A produced value
    names none, because libstore has no type shaped like the answer -
    `queryPathInfo` hands back a ValidPathInfo whose fields a caller
    reads one at a time. So the emitter declares the struct, and the
    name is the class's own."""
    return cls.decl.cxx or f"{NAMESPACE}::{cls.name}"


def _bare(cls: Class) -> str:
    """The C++ type behind a declared class, or a refusal."""
    if cls.is_words:
        # A vocabulary. The member IS the string a Nix parser takes,
        # so it crosses as one - a fact about the words rather than
        # about either binding.
        return "std::string"
    if not cls.decl.cxx and not cls.decl.built_by:
        raise TypeError(
            f"'{cls.name}' has no C++ type behind it. Only a class with "
            f"@binding(cxx=...) or @produced(by=...) can cross as one.")
    return _held(cls)


def _cxx(t: Type, known: dict[str, Class] | None = None) -> tuple[str, str | None]:
    """A declared type as C++ carries it BY VALUE, and its caster.

    The value form, not the parameter form. A return is a value, a
    vector's element is a value, and an optional's payload is a
    value - so this is the shape everything else is built from and
    `_param` adds the reference where a parameter wants one."""
    known = known or {}
    inner = t.python.removesuffix("| None").strip()
    if t.python.endswith("| None"):
        held, _ = _cxx(Type(python=inner, cxx=t.cxx, bound=t.bound), known)
        return f"std::optional<{held}>", "optional"
    if inner.startswith("list["):
        item = inner[len("list["):-1]
        held, _ = _cxx(Type(python=item, bound=item[:1].isupper()), known)
        # A vector, not the std::set libstore keeps them in. A set
        # casts to a Python set, which has no order - and every one
        # of these answers is sorted, which is information a caller
        # can use.
        return f"std::vector<{held}>", "vector"
    if t.bound or inner in known:
        if inner not in known:
            raise TypeError(
                f"'{inner}' names a class this run has not read. Pass its "
                f"declaration too, so the C++ spelling can be resolved.")
        other = known[inner]
        spelled = _bare(other)
        if other.decl.cxx and other.decl.built_by:
            # A HANDLE nothing constructs. libstore hands one back
            # reference-counted, and Python has to hold a share or
            # the store closes under the object that names it.
            return f"std::shared_ptr<{spelled}>", "shared_ptr"
        return spelled, "string" if spelled == "std::string" else None
    # Three tables, in the order the declaration meant them. A Python
    # type nanobind casts natively wins outright - `pathlib.Path`
    # carries Cxx("string") for the pxd's sake, and nanobind has a
    # filesystem caster. Then the alias, which is the declaration
    # naming a C++ spelling. Then the bare builtin, which names none.
    spelled, caster = "", None
    if inner in CXX_PYTHON:
        spelled, caster = CXX_PYTHON[inner]
    elif t.cxx is not None and t.cxx.spelling in CXX_PARAM:
        spelled, caster = CXX_PARAM[t.cxx.spelling]
    elif inner in CXX_BUILTIN:
        spelled, caster = CXX_BUILTIN[inner]
    else:
        raise TypeError(
            f"'{t.python}' has no C++ spelling. A bound class names types "
            f"through an Annotated alias in declare.py.")
    # Both tables spell a PARAMETER, so both may carry a reference.
    # A value never does: a `const std::string &` member of a struct
    # is a dangling reference waiting to happen, and an optional
    # cannot hold one at all.
    return spelled.removeprefix("const ").removesuffix(" &"), caster


def _param(t: Type, known: dict[str, Class] | None = None
           ) -> tuple[str, str | None]:
    """The C++ spelling of a declared type, and the caster it needs.

    A BOUND type - one naming another declared class - resolves
    through `known`, which maps a declared name to its C++ spelling.
    So `is_valid_path(path: StorePath)` becomes `const nix::StorePath
    &`, and neither declaration repeats the other's C++ name."""
    inner = t.python.removesuffix("| None").strip()
    if (t.python.endswith("| None") or inner.startswith("list[")
            or inner in CXX_PYTHON):
        spelled, caster = _cxx(t, known)
        # By const reference, because these are the types worth not
        # copying - and, for `nb::bytes`, because a copy would be
        # WRONG. A method that releases the GIL runs its whole body
        # with the guard held, so a by-value Python handle changes a
        # reference count without the GIL and nanobind aborts the
        # process: "attempted to change the reference count of a
        # Python object while the GIL was not held". A reference
        # binds to the caster's own object, which nanobind destroys
        # after the guard.
        if spelled.startswith("const "):
            return spelled, caster
        return f"const {spelled} &", caster
    if t.bound:
        if not known or t.python not in known:
            raise TypeError(
                f"'{t.python}' names a class this run has not read. Pass its "
                f"declaration too, so the C++ spelling can be resolved.")
        other = known[t.python]
        if other.is_words:
            # A vocabulary. The member IS the string a Nix parser
            # takes, so it crosses as one - the same crossing the
            # Cython backend makes, because it is a fact about the
            # words rather than about either binding.
            return CXX_PARAM["string"]
        return f"const {_bare(other)} &", None
    if t.cxx is None or t.cxx.spelling not in CXX_PARAM:
        raise TypeError(
            f"'{t.python}' has no C++ parameter spelling. A bound class "
            f"names types through an Annotated alias in declare.py.")
    return CXX_PARAM[t.cxx.spelling]


def includes(classes: Sequence[Class],
             functions: Sequence[Method] = (),
             known: dict[str, Class] | None = None) -> list[str]:
    """Exactly the headers this translation unit needs, and no others.

    Derived from the declared types rather than listed. A caster left
    out does not fail at compile time - nanobind fails the conversion
    at RUNTIME with a bare std::bad_cast out of module init, which is
    a bad way to learn about a missing include.

    Every declared type of every method, parameter and return alike.
    A return needs its caster as much as a parameter does, and the
    first version only walked the parameters - which held while the
    only return was a string_view and stopped the moment one was a
    vector."""
    casters: set[str] = set()

    def note(t: Type | None) -> None:
        if t is None:
            return
        try:
            _, caster = _cxx(t, known)
        except TypeError:
            # A type this emitter cannot spell is reported where it is
            # emitted, with the method that named it. Failing here
            # would name only the type.
            return
        if caster:
            casters.add(caster)
        # A container or an optional needs its ELEMENT's caster too:
        # `list[StorePath]` needs <vector>, and a `list[str]` needs
        # <string> beneath it.
        inner = t.python.removesuffix("| None").strip()
        if inner.startswith("list["):
            note(Type(python=inner[len("list["):-1],
                      bound=inner[len("list["):-1][:1].isupper()))
        elif inner != t.python:
            note(Type(python=inner, cxx=t.cxx, bound=t.bound))

    for cls in classes:
        for m in cls.methods:
            for _, t in m.params:
                note(t)
            note(m.ret)
        if cls.ctor is not None:
            for _, t in cls.ctor.params:
                note(t)
    # A free function belongs to no class, so its types reach this
    # list only from here - and `open_store` is the one that brings
    # <nanobind/stl/shared_ptr.h> in.
    for fn in functions:
        for _, t in fn.params:
            note(t)
        note(fn.ret)
    # A hook has no signature worth casting, and it still names the
    # header its C++ lives in. `wanted` below is where that lands.

    out = ["#include <nanobind/nanobind.h>"]
    out += [f"#include <nanobind/stl/{c}.h>" for c in sorted(casters)]
    if any(cls.decl.wire == "value" and cls.decl.text for cls in classes):
        # std::hash lives in <functional>, and the value hash uses it.
        out.append("#include <functional>")
    # Each class's header, then whatever the bodies reach past it.
    # Sorted and de-duplicated, because two methods needing one
    # header is normal and the order of a declaration's methods is
    # not an order for includes.
    wanted = {cls.decl.header for cls in classes}
    wanted |= {h for cls in classes for m in cls.methods for h in m.headers}
    wanted |= {h for fn in functions for h in fn.headers}
    out += [f'#include "{h}"' for h in sorted(wanted - {""})]
    return out


def waits(cls: Class, m: Method) -> bool:
    """Whether this call can block, and so needs the GIL released.

    The class states the general case and a method overrides it in
    either direction - `@blocks` on a class that mostly does not,
    `@instant` on one that mostly does. Releasing the GIL is not free:
    it costs two thread-state transitions, so doing it around a read
    of a string already in memory is a loss."""
    if m.instant:
        return False
    return cls.decl.blocking or m.blocks


def _default(pr, known: dict[str, Class] | None = None) -> str:
    """A Python default, as C++ spells the same value.

    Two cases the table cannot hold, because both need the
    declaration to resolve them.

    A VOCABULARY member is a name in Python and a string in C++:
    `HashAlgorithm.SHA256` is `"sha256"`, and only the vocabulary
    knows which. Emitting the Python spelling put an undeclared
    identifier in the C++.

    `None` on a CONTAINER is an empty one. A repeated field has no
    presence and needs none - an absent container IS an empty one,
    which is what the declaration's own docstring says - so `nullptr`
    would be a null reference where a value belongs."""
    known = known or {}
    value = pr.default
    if value is None:
        return ""
    head = value.split(".")[0]
    if head in known and known[head].is_words:
        member = value.split(".", 1)[1]
        word = next((w for w in known[head].members if w.name == member), None)
        if word is None:
            raise TypeError(
                f"{head} has no word called {member}.")
        return f'"{word.value}"'
    if absent(pr, known):
        # None, and the signature says so. The parameter arrives as a
        # std::optional and an emitted line turns it into an empty
        # container - so a caller who passes nothing and a caller who
        # passes None get the same answer, which is what the Cython
        # binding did.
        return "nb::none()"
    if value[:1] in "'\"":
        # A string literal, RE-SPELLED. Python writes one either way
        # round and `ast.unparse` normalises to single quotes - which
        # in C++ is a character literal, so `'auto'` compiles as an
        # integer rather than failing.
        return json.dumps(ast.literal_eval(value))
    return CXX_DEFAULT.get(value, value)


def _extras(cls: Class, m: Method, known: dict[str, Class] | None = None) -> str:
    """The annotations that follow a `.def`, in nanobind's order.

    `nb::call_guard<nb::gil_scoped_release>()` comes from the SAME
    declared fact that makes the Cython emitter write `with nogil:` -
    `blocking` on the class, or `@blocks` on the method. One decision,
    two spellings, and the declaration never learns which backend read
    it.

    `"name"_a` follows, because a parameter's name is part of the
    Python signature rather than decoration, and `= value` after it
    when the declaration gave a default. Dropping a default would
    silently change the signature a caller sees."""
    out = []
    if waits(cls, m):
        out.append("nb::call_guard<nb::gil_scoped_release>()")
    for pr in m.params:
        arg = f'"{pr.name}"_a'
        if pr.default is not None:
            arg += f" = {_default(pr, known)}"
        out.append(arg)
    return "".join(f", {x}" for x in out)


def _method(cls: Class, m: Method, known: dict[str, Class] | None = None
            ) -> list[str]:
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
    if m.parts:
        return _parts_method(cls, m, known or {})
    if m.cxx_body:
        # A method the declaration could not derive, carried verbatim.
        obj = _self(cls)
        args, opening = _signature(cls, m, known)
        head = (f'{INDENT * 2}.def("{m.name}", '
                f"[]({_held(cls)} &{obj}{args}) {{")
        body = [f"{INDENT * 4}{ln}".rstrip()
                for ln in m.cxx_body.strip().splitlines()]
        return [head, *opening, *body,
                f"{INDENT * 2}}}{_extras(cls, m, known)})"]
    spelled = m.cxx_name or m.name
    return [f'{INDENT * 2}.def("{m.name}", &{_held(cls)}::{spelled}'
            f"{_extras(cls, m, known)})"]


def absent(pr, known: dict[str, Class] | None = None) -> bool:
    """Whether this parameter's absence is spelled `None`.

    A CONTAINER whose declared default is None. The declaration's own
    docstring says what that means - a repeated field has no presence
    and needs none, so an absent container IS an empty one - and a
    caller passing None explicitly means the same thing.

    It matters because nanobind's vector caster refuses None: it asks
    for a sequence, and None is not one. So a parameter that reads
    None has to say so in its own type."""
    if pr.default != "None":
        return False
    inner = pr.type.python.removesuffix("| None").strip()
    return inner.startswith("list[")


def _signature(cls: Class, m: Method,
               known: dict[str, Class] | None = None
               ) -> tuple[str, list[str]]:
    """A lambda's parameter list, and the lines that open its body.

    Almost always the first alone. The lines exist for one case: a
    container that reads None. It arrives as a std::optional, and the
    body wants the plain container - so the parameter is renamed and
    one derived line puts the declared name back, holding an empty
    container where the caller passed nothing.

    The body then reads exactly what the declaration wrote."""
    args, opening = "", []
    for pr in m.params:
        spelled, _ = _param(pr.type, known)
        if not absent(pr, known):
            args += f", {spelled} {pr.name}"
            continue
        held, _ = _cxx(pr.type, known)
        args += f", const std::optional<{held}> & {pr.name}_"
        opening.append(f"{INDENT * 4}const {held} {pr.name} = "
                       f"{pr.name}_.value_or({held}{{}});")
    return args, opening


def _parts_method(cls: Class, m: Method,
                  known: dict[str, Class]) -> list[str]:
    """One `.def` that BUILDS a record and hands it back.

    `@cxx_parts` carries the call and one expression per field. The
    aggregate initialisation around them is derived, in the order the
    record declares its members - so a field added to the value moves
    the struct, the constructor, this initialiser and the accessors
    together.

    It also refuses a field map that misses a field or invents one.
    Aggregate initialisation is positional, so a map with the right
    count and the wrong names would compile and put every value in
    the wrong slot."""
    assert m.ret is not None
    target = known[m.ret.python]
    named = dict(m.parts)
    want = [name for name, _ in record_fields(target, known)]
    missing = [n for n in want if n not in named]
    extra = [n for n in named if n not in want]
    if missing or extra:
        raise TypeError(
            f"{m.name}: @cxx_parts must name every field of "
            f"{target.name} and no other. Missing: {missing or 'none'}. "
            f"Not a field: {extra or 'none'}.")
    obj = _self(cls)
    args = "".join(f", {_param(pr.type, known)[0]} {pr.name}"
                   for pr in m.params)
    body = [f"{INDENT * 4}{ln}".rstrip()
            for ln in m.parts_prelude.strip().splitlines()]
    body.append(f"{INDENT * 4}return {_held(target)}{{")
    body += [f"{INDENT * 5}{named[n]}," for n in want]
    body.append(f"{INDENT * 4}}};")
    return [f'{INDENT * 2}.def("{m.name}", '
            f"[]({_held(cls)} &{obj}{args}) {{",
            *body, f"{INDENT * 2}}}{_extras(cls, m, known)})"]


def _ctor(cls: Class, known: dict[str, Class] | None = None) -> list[str]:
    """`nb::init<...>`, with the declared parameter named for Python.

    `"name"_a` is what makes the parameter usable as a keyword, so the
    declaration's parameter NAME reaches callers rather than being
    decoration."""
    if cls.ctor is None:
        return []
    types = ", ".join(_param(t, known)[0] for _, t in cls.ctor.params)
    args = "".join(f', "{n}"_a' for n, _ in cls.ctor.params)
    line = f"{INDENT * 2}.def(nb::init<{types}>(){args}"
    if not cls.ctor.doc:
        return [line + ")"]
    # One line, however the declaration wrapped it: a C++ string
    # literal has no continuation and gluing two is noise.
    doc = " ".join(cls.ctor.doc.split()).replace("\\", "\\\\").replace('"', r'\"')
    return [line + ",", f'{INDENT * 3}     "{doc}")']


def _render(cls: Class, accessor: str) -> str:
    """The C++ that renders one of this value's accessors as text.

    The accessor names one of the class's OWN, and the emitter
    resolves it rather than the declaration spelling C++. Two steps,
    both derived:

    An accessor that `@reads` a member addresses the member, because
    there is no method to call - `vpi.path`, not `vpi.store_path()`.

    An accessor returning something other than `str` cannot render
    itself, so its own type must. `to_string` is what a declared value
    type names for that, which is how `vpi.path` becomes
    `vpi.path.to_string()` without this file knowing what a StorePath
    is."""
    obj = _self(cls)
    for m in cls.methods:
        if m.name != accessor:
            continue
        expr = f"{obj}.{m.reads}" if m.reads else f"{obj}.{m.cxx_name or m.name}()"
        if m.ret is not None and m.ret.python != "str":
            expr += ".to_string()"
        return f"std::string({expr})"
    raise TypeError(
        f"{cls.name}: \"{accessor}\" names no accessor on this class.")


def _repr_parts(cls: Class) -> str:
    """`"name='" + <read> + "'"` for every declared field, joined.

    Str fields only, and it refuses rather than guessing. A number
    would need `std::to_string` and a nested value its own repr;
    inventing either here would put a wrong answer in an emitted file
    instead of a message in this one."""
    decl = cls.decl
    fields = decl.fields or (Field(decl.shown, "str", read=decl.shown),)
    parts = []
    for i, f in enumerate(fields):
        if f.type != "str":
            raise TypeError(
                f"{cls.name}.{f.name}: a repr of a {f.type} is not derived "
                f"yet. Only str fields render without a second decision.")
        lead = ", " if i else ""
        parts.append(f'+ "{lead}{f.name}=\'" + {_render(cls, f.read)} '
                     f'+ "\'"')
    return " ".join(parts)


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
    held = _held(cls)
    ref = f"const {held} &{obj}"
    out: list[str] = []

    if decl.text:
        # A CONVERSION, and only for a value that IS a string.
        out.append(f'{INDENT * 2}.def("__str__", []({ref}) '
                   f"{{ return {_render(cls, decl.text)}; }})")
    if decl.fields or decl.shown:
        # An IDENTIFICATION, which every value owes a reader.
        #
        # From the FIELDS where there are any, because a field carries
        # both halves of what a repr says: its name, and the accessor
        # that reads it. `shown` carries only the second, so a repr
        # built from it drops the name and prints `StorePath('...')`
        # where `_value.py` prints `StorePath(base_name='...')` from
        # the same declaration. One declaration answering twice is the
        # one thing two backends must not do.
        out += [f'{INDENT * 2}.def("__repr__", []({ref}) {{',
                # std::string on the leading literal: two `const
                # char*` added with + is pointer arithmetic in C++,
                # not concatenation, and it does not compile.
                f'{INDENT * 3}return std::string("{cls.name}(") '
                f'{_repr_parts(cls)} + ")";',
                f"{INDENT * 2}}})"]

    if decl.wire == "value":
        # A value COPIES. Without these, copy.copy falls through to
        # pickle, which a bound C++ type cannot do - so a caller gets
        # TypeError where the Cython backend hands back a copy.
        #
        # A bound value is immutable, so a deep copy IS a copy. The
        # Cython emitter already says exactly that; this is the same
        # sentence in the other language.
        out += [f'{INDENT * 2}.def("__copy__", []({ref}) '
                f"{{ return {held}({obj}); }})",
                f'{INDENT * 2}.def("__deepcopy__", []({ref}, nb::dict) '
                f"{{ return {held}({obj}); }}, \"memo\"_a)"]

    facts = {"value": decl.compare == "cxx",
             "order": decl.compare == "cxx" and decl.order}
    for name, op, fact in COMPARISONS:
        if not facts[fact]:
            continue
        # The C++ comparison, not a Python one on the rendered text.
        # Upstream defaults these, so declaring them means the binding
        # follows if that ever stops being true.
        out += [f'{INDENT * 2}.def("{name}", [](const {held} &a, '
                f"const {held} &b)",
                f"{INDENT * 3} {{ return a {op} b; }}, nb::is_operator())"]

    if decl.compare == "cxx" and decl.text:
        # Only where __eq__ exists. A hash must agree with equality,
        # and a class with no declared comparison has none to agree
        # with - Python's identity hash is then the honest answer.
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
        return [f'{INDENT * 2}.def_ro("{m.name}", &{_held(cls)}::{m.reads})']
    if m.cxx_body:
        body = m.cxx_body.strip().splitlines()
        spelled = CXX_OPTIONAL.get(m.ret.python if m.ret else "", "")
        ret = f" -> {spelled}" if spelled else ""
        head = (f'{INDENT * 2}.def_prop_ro("{m.name}", '
                f"[](const {_held(cls)} &{obj}){ret} {{")
        return [head, *[f"{INDENT * 4}{ln}".rstrip() for ln in body],
                f"{INDENT * 2}}})"]
    raise TypeError(
        f"{cls.name}.{m.name}: a produced value's accessor must say what it "
        f"reads. Use @reads(\"member\") for a data member, or @cxx_body(...) "
        f"when it is computed.")


def _unused_record(cls: Class) -> list[str]:
    """A VALUE's accessors, which are its fields.

    What decides this shape is `@wire_value`, not `@produced`. The
    two were conflated once and the Store declaration caught it: a
    value is a RECORD, so Python reads its parts as attributes, while
    a proxy is a HANDLE, so Python calls its methods. `@produced`
    answers a third question - whether a constructor exists - and it
    is true of both `nix::ValidPathInfo` and `nix::Store` for
    completely different reasons.

    Unlike the Cython backend, nothing is flattened. Cython cannot
    easily hand back a C++ struct, so cythonix copies nine fields into
    Python slots; nanobind binds `nix::ValidPathInfo` itself and each
    accessor reads the live object."""
    out: list[str] = []
    for m in cls.methods:
        out += _accessor(cls, m)
    return out


def _parts_method(cls: Class, m: Method,
                  known: dict[str, Class]) -> list[str]:
    """One `.def` that BUILDS a record and hands it back.

    `@cxx_parts` carries the call and one expression per field. The
    aggregate initialisation around them is derived, in the order the
    record declares its members - so a field added to the value moves
    the struct, the constructor, this initialiser and the accessors
    together.

    It also refuses a field map that misses a field or invents one.
    Aggregate initialisation is positional, so a map with the right
    count and the wrong names would compile and put every value in
    the wrong slot."""
    assert m.ret is not None
    target = known[m.ret.python]
    named = dict(m.parts)
    want = [name for name, _ in record_fields(target, known)]
    missing = [n for n in want if n not in named]
    extra = [n for n in named if n not in want]
    if missing or extra:
        raise TypeError(
            f"{m.name}: @cxx_parts must name every field of "
            f"{target.name} and no other. Missing: {missing or 'none'}. "
            f"Not a field: {extra or 'none'}.")
    obj = _self(cls)
    args = "".join(f", {_param(pr.type, known)[0]} {pr.name}"
                   for pr in m.params)
    body = [f"{INDENT * 4}{ln}".rstrip()
            for ln in m.parts_prelude.strip().splitlines()]
    body.append(f"{INDENT * 4}return {_held(target)}{{")
    body += [f"{INDENT * 5}{named[n]}," for n in want]
    body.append(f"{INDENT * 4}}};")
    return [f'{INDENT * 2}.def("{m.name}", '
            f"[]({_held(cls)} &{obj}{args}) {{",
            *body, f"{INDENT * 2}}}{_extras(cls, m, known)})"]


def _ctor(cls: Class, known: dict[str, Class] | None = None) -> list[str]:
    """`nb::init<...>`, with the declared parameter named for Python.

    `"name"_a` is what makes the parameter usable as a keyword, so the
    declaration's parameter NAME reaches callers rather than being
    decoration."""
    if cls.ctor is None:
        return []
    types = ", ".join(_param(t, known)[0] for _, t in cls.ctor.params)
    args = "".join(f', "{n}"_a' for n, _ in cls.ctor.params)
    line = f"{INDENT * 2}.def(nb::init<{types}>(){args}"
    if not cls.ctor.doc:
        return [line + ")"]
    # One line, however the declaration wrapped it: a C++ string
    # literal has no continuation and gluing two is noise.
    doc = " ".join(cls.ctor.doc.split()).replace("\\", "\\\\").replace('"', r'\"')
    return [line + ",", f'{INDENT * 3}     "{doc}")']


def _render(cls: Class, accessor: str) -> str:
    """The C++ that renders one of this value's accessors as text.

    The accessor names one of the class's OWN, and the emitter
    resolves it rather than the declaration spelling C++. Two steps,
    both derived:

    An accessor that `@reads` a member addresses the member, because
    there is no method to call - `vpi.path`, not `vpi.store_path()`.

    An accessor returning something other than `str` cannot render
    itself, so its own type must. `to_string` is what a declared value
    type names for that, which is how `vpi.path` becomes
    `vpi.path.to_string()` without this file knowing what a StorePath
    is."""
    obj = _self(cls)
    for m in cls.methods:
        if m.name != accessor:
            continue
        expr = f"{obj}.{m.reads}" if m.reads else f"{obj}.{m.cxx_name or m.name}()"
        if m.ret is not None and m.ret.python != "str":
            expr += ".to_string()"
        return f"std::string({expr})"
    raise TypeError(
        f"{cls.name}: \"{accessor}\" names no accessor on this class.")


def _repr_parts(cls: Class) -> str:
    """`"name='" + <read> + "'"` for every declared field, joined.

    Str fields only, and it refuses rather than guessing. A number
    would need `std::to_string` and a nested value its own repr;
    inventing either here would put a wrong answer in an emitted file
    instead of a message in this one."""
    decl = cls.decl
    fields = decl.fields or (Field(decl.shown, "str", read=decl.shown),)
    parts = []
    for i, f in enumerate(fields):
        if f.type != "str":
            raise TypeError(
                f"{cls.name}.{f.name}: a repr of a {f.type} is not derived "
                f"yet. Only str fields render without a second decision.")
        lead = ", " if i else ""
        parts.append(f'+ "{lead}{f.name}=\'" + {_render(cls, f.read)} '
                     f'+ "\'"')
    return " ".join(parts)


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
    held = _held(cls)
    ref = f"const {held} &{obj}"
    out: list[str] = []

    if decl.text:
        # A CONVERSION, and only for a value that IS a string.
        out.append(f'{INDENT * 2}.def("__str__", []({ref}) '
                   f"{{ return {_render(cls, decl.text)}; }})")
    if decl.fields or decl.shown:
        # An IDENTIFICATION, which every value owes a reader.
        #
        # From the FIELDS where there are any, because a field carries
        # both halves of what a repr says: its name, and the accessor
        # that reads it. `shown` carries only the second, so a repr
        # built from it drops the name and prints `StorePath('...')`
        # where `_value.py` prints `StorePath(base_name='...')` from
        # the same declaration. One declaration answering twice is the
        # one thing two backends must not do.
        out += [f'{INDENT * 2}.def("__repr__", []({ref}) {{',
                # std::string on the leading literal: two `const
                # char*` added with + is pointer arithmetic in C++,
                # not concatenation, and it does not compile.
                f'{INDENT * 3}return std::string("{cls.name}(") '
                f'{_repr_parts(cls)} + ")";',
                f"{INDENT * 2}}})"]

    if decl.wire == "value":
        # A value COPIES. Without these, copy.copy falls through to
        # pickle, which a bound C++ type cannot do - so a caller gets
        # TypeError where the Cython backend hands back a copy.
        #
        # A bound value is immutable, so a deep copy IS a copy. The
        # Cython emitter already says exactly that; this is the same
        # sentence in the other language.
        out += [f'{INDENT * 2}.def("__copy__", []({ref}) '
                f"{{ return {held}({obj}); }})",
                f'{INDENT * 2}.def("__deepcopy__", []({ref}, nb::dict) '
                f"{{ return {held}({obj}); }}, \"memo\"_a)"]

    facts = {"value": decl.compare == "cxx",
             "order": decl.compare == "cxx" and decl.order}
    for name, op, fact in COMPARISONS:
        if not facts[fact]:
            continue
        # The C++ comparison, not a Python one on the rendered text.
        # Upstream defaults these, so declaring them means the binding
        # follows if that ever stops being true.
        out += [f'{INDENT * 2}.def("{name}", [](const {held} &a, '
                f"const {held} &b)",
                f"{INDENT * 3} {{ return a {op} b; }}, nb::is_operator())"]

    if decl.compare == "cxx" and decl.text:
        # Only where __eq__ exists. A hash must agree with equality,
        # and a class with no declared comparison has none to agree
        # with - Python's identity hash is then the honest answer.
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
        return [f'{INDENT * 2}.def_ro("{m.name}", &{_held(cls)}::{m.reads})']
    if m.cxx_body:
        body = m.cxx_body.strip().splitlines()
        spelled = CXX_OPTIONAL.get(m.ret.python if m.ret else "", "")
        ret = f" -> {spelled}" if spelled else ""
        head = (f'{INDENT * 2}.def_prop_ro("{m.name}", '
                f"[](const {_held(cls)} &{obj}){ret} {{")
        return [head, *[f"{INDENT * 4}{ln}".rstrip() for ln in body],
                f"{INDENT * 2}}})"]
    raise TypeError(
        f"{cls.name}.{m.name}: a produced value's accessor must say what it "
        f"reads. Use @reads(\"member\") for a data member, or @cxx_body(...) "
        f"when it is computed.")


def _unused_record(cls: Class) -> list[str]:
    """A VALUE's accessors, which are its fields.

    What decides this shape is `@wire_value`, not `@produced`. The
    two were conflated once and the Store declaration caught it: a
    value is a RECORD, so Python reads its parts as attributes, while
    a proxy is a HANDLE, so Python calls its methods. `@produced`
    answers a third question - whether a constructor exists - and it
    is true of both `nix::ValidPathInfo` and `nix::Store` for
    completely different reasons.

    Unlike the Cython backend, nothing is flattened. Cython cannot
    easily hand back a C++ struct, so cythonix copies nine fields into
    Python slots; nanobind binds `nix::ValidPathInfo` itself and each
    accessor reads the live object."""
    out: list[str] = []
    for m in cls.methods:
        out += _accessor(cls, m)
    return out


def record_fields(cls: Class,
                  known: dict[str, Class] | None = None
                  ) -> list[tuple[str, str]]:
    """Every member of a produced value's struct, in declared order.

    Nothing is listed. A produced value's ACCESSORS are its fields -
    the store flattened one of its own objects and handed over the
    parts - so the name is the accessor's name and the type is what
    it returns. Order is the declaration's, which is the order a
    reader of the declaration sees and the order the constructor
    takes."""
    return [(m.name, _cxx(m.ret, known)[0])
            for m in cls.methods if m.ret is not None]


def _paragraph(doc: str) -> list[str]:
    """The first paragraph of a docstring, as its lines."""
    out: list[str] = []
    for line in doc.strip().splitlines():
        if not line.strip():
            break
        out.append(line.strip())
    return out


def record(cls: Class, known: dict[str, Class] | None = None) -> list[str]:
    """The C++ struct a produced value crosses as.

    Real types, every one. This is where the two backends part
    company hardest: a pxd cannot declare a std::optional, a std::set
    or a member with no default constructor, so the Cython emitter
    turned a store path into its base name, absence into an empty
    string and a set into a vector of strings - and Python then held
    nine slots that had each been printed and re-parsed on the way.

    Here a `nix::StorePath` stays one, `std::optional` carries
    absence, and a list of paths is a list of paths. Nothing is
    printed and nothing is parsed back.

    An aggregate, so the constructor is the member list in order and
    C++20's parenthesised aggregate initialisation gives `nb::init`
    something to call. `operator==` is defaulted rather than written,
    which is what makes the value compare as its parts."""
    # The first PARAGRAPH, on one line. A first line alone can stop
    # mid-sentence, because the declaration wraps its prose for a
    # reader rather than for this.
    lead = " ".join(_paragraph(cls.doc))
    out = [f"/** {lead} */", f"struct {cls.name}", "{"]
    out += [f"{INDENT}{spelling} {name};"
            for name, spelling in record_fields(cls, known)]
    # A value compares as its parts, and `= default` is the whole of
    # that sentence. Written out, it would be one line per field with
    # nothing to gate it against the field list.
    out += [f"{INDENT}bool operator==(const {cls.name} &) const = default;",
            "};"]
    return out


def records(classes: Sequence[Class],
            known: dict[str, Class] | None = None) -> list[str]:
    """Every produced value in one unit, inside one namespace."""
    values = [c for c in classes if c.is_value]
    if not values:
        return []
    out = [f"namespace {NAMESPACE} {{", ""]
    if any(_lists(cls) for cls in values):
        out += [*HASHABLE.strip().splitlines(), ""]
    for cls in values:
        out += [*record(cls, known), ""]
    return [*out, f"}}  // namespace {NAMESPACE}", ""]


def _lists(cls: Class) -> list[str]:
    """The declared fields of this value that are lists."""
    return [m.name for m in cls.methods
            if m.ret is not None and m.ret.python.startswith("list[")]


# A list field, as something a hash can hold.
#
# One function rather than a cast at each call site, because
# `nb::tuple(h.attr("x"))` does not resolve: `attr` hands back an
# accessor, and the constructor that CONVERTS takes a handle. Passing
# it as an argument does the conversion the constructor would not.
HASHABLE = """
/** A list field as the tuple a hash can hold. Order is the list's. */
inline nb::tuple as_tuple(nb::handle items)
{
    return nb::tuple(items);
}
"""


def _record_semantics(cls: Class,
                      known: dict[str, Class] | None = None) -> list[str]:
    """What a RECORD owes Python, beyond reading its own fields.

    A repr, an equality and a hash, and all three derived from the
    field list rather than from a type table. The trick is that each
    one goes through the PYTHON object: `nb::repr(h.attr("path"))`
    asks StorePath for its repr, so a field of any bound type renders
    without this emitter knowing anything about it. The Cython
    backend gets the same three from `_value.py` and the same
    declaration.

    `__ne__` is not here and does not need to be. Python fills the
    slot as soon as `__eq__` exists."""
    declared = [(m.name, m.ret.python) for m in cls.methods
                if m.ret is not None]
    fields = [name for name, _ in declared]
    held = _held(cls)
    spec = ", ".join(f"{name}={{!r}}" for name in fields)
    reads = ", ".join(f'h.attr("{name}")()' for name in fields)
    # A LIST field becomes a tuple before it is hashed. A list is
    # unhashable for the good reason that it can change, and this one
    # cannot - it is a copy of what the store said. `_value.py` makes
    # the same substitution from the same declaration, so the two
    # backends hash the same thing.
    hashed = ", ".join(
        (f'{NAMESPACE}::as_tuple(h.attr("{name}")())'
         if python.startswith("list[")
         else f'h.attr("{name}")()')
        for name, python in declared)
    return [
        # An IDENTIFICATION, which every value owes a reader. The
        # same shape `_value.py` writes: the class name, then every
        # field as `name=<repr>`.
        f'{INDENT * 2}.def("__repr__", [](nb::handle h) {{',
        f'{INDENT * 3}return nb::str("{cls.name}({spec})").format(',
        f"{INDENT * 4}{reads});",
        f"{INDENT * 2}}})",
        # Equality is the struct's, which is its members'.
        f'{INDENT * 2}.def("__eq__", [](const {held} &a, const {held} &b)',
        f"{INDENT * 3} {{ return a == b; }}, nb::is_operator())",
        # And the hash agrees with it, because it hashes the same
        # fields in the same order. A tuple, so Python does the
        # combining - which is one fewer thing to get subtly wrong
        # than a hand-rolled mix.
        f'{INDENT * 2}.def("__hash__", [](nb::handle h) {{',
        f"{INDENT * 3}return nb::hash(nb::make_tuple({hashed}));",
        f"{INDENT * 2}}})",
    ]


def _record_ctor(cls: Class, known: dict[str, Class] | None = None
                 ) -> list[str]:
    """The two ways a record is and is not built.

    A produced value is PRODUCED. Nothing a caller does should build
    one from nothing, and `__init__` says so in the sentence
    `@produced(by=...)` supplied - so a caller who guesses wrong is
    told where to look instead of getting an argument-count error.

    It still has to be RECONSTRUCTIBLE, because it crosses the wire
    and the far side has only the parts. So the constructor is bound
    privately, as `_from_parts`, which is exactly the name the wire
    layer asks for. The Cython backend arrives at the same pair from
    the other direction: an `__init__` that raises, and a classmethod
    that goes through `__new__`."""
    fields = record_fields(cls, known)
    held = _held(cls)
    args = "".join(f', "{name}"_a' for name, _ in fields)
    made = ", ".join(f"{spelling} {name}" for name, spelling in fields)
    values = ", ".join(name for name, _ in fields)
    return [
        f'{INDENT * 2}.def("__init__", []({held} *) {{',
        f"{INDENT * 3}throw nb::type_error(",
        f'{INDENT * 4}"{cls.name} objects come from {cls.decl.built_by}, '
        f'not from a constructor");',
        f"{INDENT * 2}}})",
        f'{INDENT * 2}.def_static("_from_parts", []({made}) {{',
        f"{INDENT * 3}return {held}{{{values}}};",
        f'{INDENT * 2}}}{args}, "{FROM_PARTS_DOC}")',
    ]


# What `_from_parts` is for, in the Cython emitter's own words.
FROM_PARTS_DOC = "Wire-deserialization helper (private, never surfaced)."


def _factory(cls: Class, functions: Sequence[Method],
             known: dict[str, Class] | None = None) -> list[str]:
    """`nb::new_`, for a class something else makes.

    nix::Store is abstract and its implementation is chosen by a URI,
    so there is no constructor to bind - and `Store(uri)` is still the
    Python surface, because that is what the declaration's `__init__`
    says. `nb::new_` is exactly that shape: a factory returning a
    handle, bound as `__new__`.

    Both halves are declared, in two places that already had to
    agree. `@produced(by="open_store")` names the factory by its
    PYTHON name; the free function called `open_store` names the C++
    it binds. So this resolves one through the other and neither
    declaration repeats the other's spelling."""
    if cls.ctor is None:
        return []
    made = next((f for f in functions if f.name == cls.decl.built_by), None)
    if made is None:
        # A factory this declaration does not carry. The class is
        # still bound; it just offers no way in, which is the honest
        # answer until the factory is declared too.
        return []
    args = "".join(f', "{n}"_a' for n, _ in cls.ctor.params)
    line = f"{INDENT * 2}.def(nb::new_(&{made.binds}){args}"
    if not cls.ctor.doc:
        return [line + ")"]
    # One line, however the declaration wrapped it: a C++ string
    # literal has no continuation and gluing two is noise.
    doc = " ".join(cls.ctor.doc.split()).replace("\\", "\\\\").replace('"', r'\"')
    return [line + ",", f'{INDENT * 3}     "{doc}")']


def wire_fields(cls: Class) -> list[tuple[str, str, str]]:
    """What this value is made of, as (name, wire type, how to read).

    Two sources, and which one applies is a real difference. A
    CONSTRUCTED value declares its fields, because the field name and
    the accessor need not agree: a StorePath's part is called
    `base_name` and is read by CALLING `to_string()`. A PRODUCED one
    declares none and needs none - every accessor IS a field, and the
    record binds each as an attribute, so the name reads it.

    The third element carries that difference: a Python expression on
    a handle called `h`, either an attribute or a call."""
    if cls.decl.fields:
        return [(f.name, f.type, f'h.attr("{f.read}")()')
                for f in cls.decl.fields]
    if not cls.is_value:
        return []
    return [(m.name, m.ret.wire, f'h.attr("{m.name}")()')
            for m in cls.methods if m.ret is not None]


# What `_parts` is for, in the words the Cython emitter already uses.
# One sentence in two backends, so a caller reading either sees the
# same thing.
PARTS_DOC = ("Wire-serialization helper (private): one value per "
             "_wire_fields entry, in order.")


def _round_trip(cls: Class) -> list[str]:
    """`_parts`, the half of the wire round trip that is a method.

    The other half is `_from_parts`, and it needs no code at all: see
    `markers`.

    Through the PYTHON object, like the repr and the hash beside it.
    A part of any bound type comes back as whatever that type's own
    binding hands over, so this emitter never has to know what a part
    IS - which is what lets one line cover a str, a StorePath and a
    list of them."""
    fields = wire_fields(cls)
    if not fields:
        return []
    reads = ", ".join(read for _, _, read in fields)
    return [f'{INDENT * 2}.def("_parts", [](nb::handle h) {{',
            f"{INDENT * 3}return nb::make_tuple({reads});",
            f'{INDENT * 2}}}, "{PARTS_DOC}")']


def markers(cls: Class) -> list[str]:
    """The facts every layer above reads off the compiled class.

    `emit.py` writes the same set into the pyx as class attributes,
    from the same declaration. This is that sentence in the other
    language, and it is what lets the generated layer - the async
    wrappers, the protocols, the RPC stubs, the stubs - keep working
    when the class underneath stops being Cython.

    `_binds` is NOT here, and its absence is information. It names the
    pxd declaration a pyx class binds, so it is a fact about Cython
    rather than about the binding: there is no pxd, so there is
    nothing to name.

    `_wire_fields` comes from wherever the value keeps them. A
    CONSTRUCTED value declares its fields, because the field name and
    the accessor need not agree. A PRODUCED one declares none and
    needs none: every accessor IS a field."""
    decl = cls.decl
    out = [f'{INDENT}cls.attr("_threading") = "{decl.threading}";',
           f'{INDENT}cls.attr("_blocking") = '
           f'{"true" if decl.blocking else "false"};']
    # The EFFECTIVE value, not the declared one. "proxy" is the safe
    # default on both sides - stateful until a declaration proves
    # otherwise - and writing it out means a reader of the compiled
    # class is told rather than left to know the default.
    out.append(f'{INDENT}cls.attr("_wire") = "{decl.wire or "proxy"}";')
    if cls.is_value:
        out.append(f'{INDENT}cls.attr("_produced") = true;')
    elif decl.built_by:
        # Which free function makes one. A handle rather than a
        # value: a value is produced and has no factory to name.
        out.append(f'{INDENT}cls.attr("_ctor_from") = "{decl.built_by}";')
    fields = wire_fields(cls)
    if fields:
        pairs = ", ".join(f'nb::make_tuple("{n}", "{t}")'
                          for n, t, _ in fields)
        out.append(f'{INDENT}cls.attr("_wire_fields") = '
                   f"nb::make_tuple({pairs});")
        if not cls.is_value:
            # The other half of the round trip, and for a CONSTRUCTED
            # value it is the class.
            #
            # `_from_parts` takes one value per _wire_fields entry, in
            # order, and hands back the value they make. That is
            # exactly what the constructor takes - the Cython emitter
            # refuses to derive the helper unless those two agree - so
            # naming the class is the whole helper, and it cannot
            # drift from the constructor.
            #
            # A PRODUCED value has no public constructor to name: its
            # `__init__` raises, and `_from_parts` is bound beside it
            # as a static method.
            out.append(f'{INDENT}cls.attr("_from_parts") = cls;')
    return out


def bind_function(cls: Class, known: dict[str, Class] | None = None,
                  functions: Sequence[Method] = ()) -> str:
    """The whole `bind_<name>` function for one declared class.

    A function per class, because that is the seam nanopynix already
    has: `nanopynix_module.cpp` calls `nanopynix_bind_store(store)`
    and friends. Generated code drops in beside hand-written code, one
    class at a time, and NB_MODULE does not change."""
    decl = cls.decl
    if not decl.cxx and not cls.is_value:
        raise TypeError(
            f"{cls.name}: no C++ type to bind. @binding(cxx=...) names it.")
    held = _held(cls)
    lines = [f"static void bind_{cls.name.lower()}(nb::module_ &m) {{",
             f'{INDENT}auto cls = nb::class_<{held}>(m, "{cls.name}")']
    if cls.is_value:
        # A RECORD: the emitter declared the struct, so every accessor
        # is a member and the whole binding is derived from the field
        # list.
        body = _record_ctor(cls, known)
        obj = _self(cls)
        # METHODS, not `def_ro` properties. The declaration writes
        # `def path(self) -> StorePath`, and the Cython backend
        # renders that as a method - so a caller writes `info.path()`
        # on either. A property would read better and would be a
        # DIFFERENT surface, which is not a choice an emitter makes
        # on its own.
        body += [f'{INDENT * 2}.def("{name}", [](const {held} &{obj}) '
                 f"{{ return {obj}.{name}; }})"
                 for name, _ in record_fields(cls, known)]
        body += _record_semantics(cls, known)
        body += _round_trip(cls)
        body += _value_semantics(cls)
        if body:
            body[-1] += ";"
        return "\n".join([*lines, *body, *markers(cls), "}"]) + "\n"
    # No nb::init when something else builds one: there is no
    # constructor to call. A FACTORY takes its place where the
    # declaration names one.
    body = (_factory(cls, functions, known) if decl.built_by
            else _ctor(cls, known))
    for m in cls.methods:
        body += _accessor(cls, m) if m.prop else _method(cls, m, known)
    body += _round_trip(cls) if decl.wire == "value" else []
    body += _value_semantics(cls)
    for source in decl.custom.values():
        body += [f"{INDENT * 2}{line}".rstrip()
                 for line in source.splitlines()]
    if body:
        body[-1] += ";"
    return "\n".join([*lines, *body, *markers(cls), "}"]) + "\n"


def free_function(fn: Method, known: dict[str, Class] | None = None) -> list[str]:
    """One `m.def`, for a function that belongs to no class.

    nanopynix has 72 of these and they are one shape:
    `m.def("open_store", &open_store_uri, "uri"_a)`. The C++ helper is
    hand-written - `open_store_uri` keeps a per-state-directory cache,
    because two LocalStores in one process deadlock on a temp-roots
    flock - and the declaration names it rather than pretending to
    have written it.

    `blocking` has no class to come from here, so a free function says
    `@blocks` for itself."""
    if not fn.binds:
        raise TypeError(
            f"{fn.name}: a free function names the C++ it binds. "
            f'Use @binds("cxx_name").')
    extras = []
    if fn.blocks and not fn.instant:
        extras.append("nb::call_guard<nb::gil_scoped_release>()")
    for pr in fn.params:
        arg = f'"{pr.name}"_a'
        if pr.default is not None:
            arg += f" = {_default(pr, known)}"
        extras.append(arg)
    tail = "".join(f", {x}" for x in extras)
    return [f'{INDENT}m.def("{fn.name}", &{fn.binds}{tail});']


def public(fns: Sequence[Method],
           classes: Sequence[Class]) -> tuple[Method, ...]:
    """The free functions the MODULE exports.

    A function some class names as its factory is not one. It is
    bound as that class's `__new__` instead, so `Store(uri)` is the
    one way in - and exporting it beside that would be a second
    spelling of the same call.

    Derived from `@produced(by=...)`, which already had to name it.
    A declaration that wants the function public as well says so by
    not naming it there."""
    made = {cls.decl.built_by for cls in classes if cls.ctor is not None}
    return tuple(fn for fn in fns if fn.name not in made)


def free_functions(fns: tuple[Method, ...],
                   known: dict[str, Class] | None = None) -> str:
    """Every free binding, in one function the module can call.

    The same seam a class gets. nanopynix's NB_MODULE already calls
    `nanopynix_bind_store(store)` and friends, so generated free
    functions arrive the same way hand-written ones do."""
    body: list[str] = []
    for fn in fns:
        body += free_function(fn, known)
    return "\n".join(["static void bind_functions(nb::module_ &m) {",
                       *body, "}"]) + "\n"


# The two conversions a CONTAINER needs, and the only ones.
#
# libstore answers with std::set and takes std::set; the binding
# hands Python a list, because these answers are sorted and a Python
# set would throw that away. Both directions are one line of C++, and
# neither copies a string or parses a name - which is the whole
# difference from what a pxd forced.
CONTAINERS = """
/** A libstore set as the list a caller reads. Sorted, because the set is. */
template <typename T>
inline std::vector<typename T::value_type> as_list(const T & items)
{
    return {items.begin(), items.end()};
}

/** The mirror: a list as the set libstore takes. */
template <typename T, typename I>
inline T as_set(const I & items)
{
    return {items.begin(), items.end()};
}

/** A set of printable values as the strings a caller reads. */
template <typename T>
inline std::vector<std::string> to_strings(const T & items)
{
    std::vector<std::string> out;
    out.reserve(items.size());
    for (auto & item : items)
        out.push_back(item.to_string());
    return out;
}
"""


def _crosses_container(classes: Sequence[Class]) -> bool:
    """Whether any declared type here is a list of bound values."""
    for cls in classes:
        for m in cls.methods:
            spelled = [t.python for _, t in m.params]
            if m.ret is not None:
                spelled.append(m.ret.python)
            for one in spelled:
                inner = one.removesuffix("| None").strip()
                if inner.startswith("list[") and inner[5:-1][:1].isupper():
                    return True
    return False


def bindable(mod: Module) -> tuple[Class, ...]:
    """The classes in this declaration nanobind can bind today.

    A vocabulary has no C++ object, so there is nothing to bind: it
    crosses as the string its member already is. A produced value has
    no C++ type either - `@cxx_parts` flattens a libstore object into
    slots, which is the shape a pxd forced - so it has no
    `nb::class_` to be until the declaration names the type it came
    from.

    Skipping is honest here rather than quiet, because
    `generate_nb.py` prints what it left out beside what it wrote."""
    return tuple(c for c in mod.classes if c.decl.cxx or c.is_value)


def module(classes: Sequence[Class],
           functions: Sequence[Method] = (),
           known: dict[str, Class] | None = None) -> str:
    """One translation unit: the includes, then a bind function each.

    Several classes, not one. A declaration file owns a module and
    may declare more than one class in it - `decl/store.py` declares
    three - and a nanobind extension is one translation unit, so the
    file and the unit are the same grain."""
    head = [*includes(classes, functions, known), "",
            "namespace nb = nanobind;",
            "using namespace nb::literals;", ""]
    if _crosses_container(classes):
        head += [*CONTAINERS.strip().splitlines(), ""]
    # The structs first: a bind function returns one, so the type has
    # to be complete before the compiler reads the lambda.
    head += records(classes, known)
    out = "\n".join(head) + "\n" + "\n".join(
        bind_function(cls, known, functions) for cls in classes)
    exported = public([fn for fn in functions
                       if not (fn.startup or fn.translator)], classes)
    return out + ("\n" + free_functions(exported, known)
                  if exported else "")


def imports(mod: Module) -> list[str]:
    """The other extensions whose types this one names.

    nanobind keeps ONE type registry for the whole process, so a
    `nix::StorePath` bound in `path` is the same Python class when
    `store` returns one - but only if `path` has been imported, and
    an extension cannot rely on a caller to have done that.

    So the module imports what it needs, and the list is derived: a
    class arrives through `mod.uses` only because the declaration
    imported it, and it needs binding only if it has C++ behind it. A
    vocabulary is filtered out here, which is why importing
    `HashAlgorithm` costs nothing."""
    return sorted({c.module for c in mod.uses.values()
                   if c.decl.cxx and c.module != mod.name})


def extension(mod: Module, dotted: str,
              known: dict[str, Class] | None = None) -> str:
    """One whole extension module: includes, bindings, entry point.

    `module` stops at the `bind_<name>` functions because that is the
    seam a project with a hand-written NB_MODULE needs. This goes the
    last step and writes the NB_MODULE too, which is what a module
    with nothing hand-written about it requires.

    The two are one line apart on purpose. A project adopting this
    gradually keeps its own entry point and calls the generated bind
    functions; a project that has finished takes this.

    `dotted` is where the build puts the module - `path` on its own,
    or `cythonix_bindings.path` inside a package. It is the one fact
    here no declaration carries, and it is what turns a sibling
    declaration's name into an import a running interpreter can
    follow."""
    known = known or mod.known
    classes = bindable(mod)
    package = dotted.rpartition(".")[0]
    reached = [f'{INDENT}nb::module_::import_("'
               f'{f"{package}." if package else ""}{stem}");'
               for stem in imports(mod)]
    translators = [translator(fn) for fn in mod.translators]
    return "\n".join([
        module(classes, mod.functions, known),
        *translators,
        f"NB_MODULE({dotted.rpartition('.')[2]}, m) {{",
        # A startup hook goes ahead of everything, imports included -
        # an import runs another module's initialisation, and a
        # library that demands initialisation is entitled to it
        # before any of its code runs. libstore does not raise when
        # it has not been initialised; it aborts the process.
        *[f"{INDENT}{fn.binds}();" for fn in mod.startup],
        # Then the other modules: a signature naming a type from one
        # of them is built as the binding is defined, so the class has
        # to already be registered.
        *reached,
        *[f"{INDENT}register_{fn.name.lstrip('_')}();"
          for fn in mod.translators],
        *[f"{INDENT}bind_{cls.name.lower()}(m);" for cls in classes],
        *([f"{INDENT}bind_functions(m);"]
          if public(mod.exported, classes) else []),
        "}",
        "",
    ])


def translator(fn: Method) -> str:
    """The module's exception translator, registered once.

    Cython wrote `except +translate_nix_error` on every method in the
    pxd, so the hook ran per call. nanobind registers a translator
    ONCE for the module and it runs for any binding in it. Same fact,
    two spellings, and the C++ is shared rather than emitted twice:
    `errors.hpp` maps nix::BadStorePath onto
    cythonix_bindings.errors.BadStorePath and strips libstore's
    terminal escapes.

    Which C++ - or whether there is any - is the declaration's to
    say. A library that throws plain std::exception needs none:
    nanobind already maps that to RuntimeError, which is what the
    mock relies on."""
    return f"""
static void register_{fn.name.lstrip("_")}() {{
    nb::register_exception_translator(
        [](const std::exception_ptr &p, void *) {{
            try {{
                std::rethrow_exception(p);
            }} catch (...) {{
                // Sets the Python error from inside catch(...), which
                // is the same position Cython called it from.
                {fn.binds}();
            }}
        }});
}}
"""


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
