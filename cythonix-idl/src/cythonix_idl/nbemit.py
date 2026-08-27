"""
Declaration -> nanobind C++.

The one backend, and the reason the declaration exists. It reads a
`read.Class` and writes one C++ function. The declaration is not C++
and does not know it is being turned into any: an emitter reads it,
and the declaration never learns which one.

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
# This is what decided the backend. The Cython route could declare
# none of these - a pxd has no std::set, no std::optional and no
# non-default-constructible member - so it turned every one of them
# into a string and parsed it back. nanobind casts them, so a store
# path stays a store path from libstore to Python.
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
# to Python and both carry Cxx("string"), which is the coarsest of
# the three answers - so a table that let either side win outright
# would turn one of them into the wrong crossing.
CXX_BUILTIN = {
    "str": ("const std::string &", "string"),
    "bool": ("bool", None),
    # A dict built by a body, handed straight to Python. There is no
    # C++ type behind it and none is wanted: the body says what goes
    # in, and nanobind's own dict is already a Python object.
    "dict[str, int]": ("nb::dict", None),
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
        if other.decl.holder:
            # Held through something. `@binding(holder="shared_ptr")`
            # is the declaration saying the factory hands back a
            # reference-counted handle, so Python has to keep a share
            # or the object closes under the name for it.
            return (f"std::{other.decl.holder}<{spelled}>",
                    other.decl.holder)
        return spelled, "string" if spelled == "std::string" else None
    # Three tables, in the order the declaration meant them. A Python
    # type nanobind casts natively wins outright - `pathlib.Path`
    # carries Cxx("string"), the coarse answer, and nanobind has a
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
            # takes, so it crosses as one. That is a fact about the
            # words rather than about the binding, which is why the
            # emitted module is plain Python with no C++ at all.
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
    if any(_overridable(cls) for cls in classes):
        # NB_TRAMPOLINE and NB_OVERRIDE live here, not in the main
        # header. Derived like every other include: a declaration that
        # marks no method @virtual needs no trampoline and does not
        # get this line.
        out.append("#include <nanobind/trampoline.h>")
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
    if value == "None" and pr.type.python.endswith("| None"):
        # An optional parameter, absent. `nullptr` is what CXX_DEFAULT
        # would give and it is a null POINTER, which a std::optional
        # parameter cannot take.
        return "nb::none()"
    if absent(pr, known):
        # None, and the signature says so. The parameter arrives as a
        # std::optional and an emitted line turns it into an empty
        # container - so a caller who passes nothing and a caller who
        # passes None get the same answer.
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

    `nb::call_guard<nb::gil_scoped_release>()` comes from one declared
    fact: `blocking` on the class, or `@blocks` on the method. The
    declaration says a call can wait; how a backend spells the release
    is the emitter's business.

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


def _parsed_by(t: Type | None, known: dict[str, Class] | None) -> str:
    """The C++ that turns this vocabulary's string into its type.

    `@words(parsed_by=...)` names it, and it was prose until this read
    it: both `add_*` methods hand-wrote
    `nix::ContentAddressMethod::parse(method)` in their bodies while
    the declaration two files away already said what the parser is
    called. Empty for anything that is not a vocabulary, and for a
    vocabulary that declares no parser - which crosses as its string
    and is parsed by whatever it is handed to."""
    if t is None or not known:
        return ""
    other = known.get(t.python.removesuffix("| None").strip())
    if other is None or not other.is_words:
        return ""
    return other.decl.parsed_by


def _handle(t: Type | None, known: dict[str, Class] | None) -> Class | None:
    """The declared class behind this type, when it binds a HANDLE.

    A handle is a class whose `@binding` carries `via`: the bound C++
    type owns a lifetime and the object worth calling is one step
    further in. `None` for everything else, which is almost every
    type - a bound class that binds its own methods is not a handle,
    and neither is a str."""
    if t is None or not t.bound or not known:
        return None
    other = known.get(t.python.removesuffix("| None").strip())
    return other if other is not None and other.decl.via else None


def _derived(cls: Class, m: Method, known: dict[str, Class] | None = None
             ) -> list[str] | None:
    """The body of a method the emitter can write itself, or None.

    Three mechanical things a HANDLE forces, and each of them was a
    verbatim `@cxx_body` before this existed:

    - the CALL goes through the handle - `v.get()->type_name()`;
    - a RETURN of a handle class wraps in it - the C++ hands back
      what it holds, and Python must get the handle;
    - a PARAMETER of a handle class unwraps out of it, because the
      C++ takes what the handle points at.

    None when this method needs none of the three. `_method` then
    binds it by pointer, which is the shorter and better line."""
    ret_handle = _handle(m.ret, known)
    args = [(pr.name, _handle(pr.type, known)) for pr in m.params]
    if not (cls.decl.via or ret_handle or any(h for _, h in args)):
        return None
    obj = _self(cls)
    reach = f"{obj}.{cls.decl.via}->" if cls.decl.via else f"{obj}."
    passed = ", ".join(f"{name}.{h.decl.via}" if h else name
                       for name, h in args)
    call = f"{reach}{m.cxx_name or m.name}({passed})"
    if m.ret is None:
        return [f"{INDENT * 4}{call};"]
    if ret_handle is not None:
        return [f"{INDENT * 4}return {_held(ret_handle)}({call});"]
    # A width the DECLARATION spells. `size()` answers a size_t and
    # the declaration says I64, so the cast is what makes the emitted
    # C++ say what the declaration says rather than what this
    # library's version of the call happens to return.
    spelled, _ = _cxx(m.ret, known)
    if spelled in ("std::int64_t", "std::uint64_t"):
        return [f"{INDENT * 4}return static_cast<{spelled}>({call});"]
    return [f"{INDENT * 4}return {call};"]


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
    # A method may return nothing - `force` and the builders' setters
    # do - and a lambda with no return statement is void. Only the
    # branches that SPELL the return type need one.
    if m.parts:
        assert m.ret is not None
        return _parts_method(cls, m, known or {})
    if m.cxx_body:
        # A method the declaration could not derive, carried verbatim.
        obj = _self(cls)
        args, opening = _signature(cls, m, known)
        # An OPTIONAL return needs its type spelled. A lambda with two
        # return paths - the value and std::nullopt - cannot deduce
        # one, and the declaration already said which type it is.
        ret = ""
        if m.ret is not None and m.ret.python.endswith("| None"):
            ret = f" -> {_cxx(m.ret, known)[0]}"
        head = (f'{INDENT * 2}.def("{m.name}", '
                f"[]({_held(cls)} &{obj}{args}){ret} {{")
        body = [f"{INDENT * 4}{ln}".rstrip()
                for ln in m.cxx_body.strip().splitlines()]
        return [head, *opening, *body,
                f"{INDENT * 2}}}{_extras(cls, m, known)})"]
    derived = _derived(cls, m, known)
    if derived is not None:
        obj = _self(cls)
        args, opening = _signature(cls, m, known)
        return [f'{INDENT * 2}.def("{m.name}", '
                f"[]({_held(cls)} &{obj}{args}) {{",
                *opening, *derived,
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
        parser = _parsed_by(pr.type, known)
        if parser:
            # A VOCABULARY arrives as the string libstore's parser
            # takes, and one call turns it into the C++ type. The
            # spelling of that call is declared once, by `@words`, so
            # a body writes the parameter name and gets the parsed
            # value - and a second method taking the same vocabulary
            # cannot spell the parse differently.
            args += f", {spelled} {pr.name}_"
            opening.append(f"{INDENT * 4}const auto {pr.name} = "
                           f"{parser}({pr.name}_);")
            continue
        if not absent(pr, known):
            args += f", {spelled} {pr.name}"
            continue
        held, _ = _cxx(pr.type, known)
        args += f", const std::optional<{held}> & {pr.name}_"
        opening.append(f"{INDENT * 4}const {held} {pr.name} = "
                       f"{pr.name}_.value_or({held}{{}});")
    return args, opening


def _identity_semantics(cls: Class,
                        known: dict[str, Class] | None = None,
                        equality: bool = True) -> list[str]:
    """The repr and the hash every wire value owes a reader.

    Both from the declared PARTS, and both through the Python object.
    `nb::repr(h.attr("path")())` asks MockStorePath for its own repr,
    so a part of any type renders without this emitter knowing what it
    is - which is what lets one line cover a str, a store path and a
    list of them.

    A list part is hashed as a TUPLE. A list is unhashable for the
    good reason that it can change, and this one cannot: it is a copy
    of what the object said.

    The hash agrees with equality because it hashes the same parts in
    the same order, and equality is either those parts or a C++
    `operator==` over the members they are read from.

    `equality=False` for a RECORD, whose `__eq__` came from
    `_record_semantics` one line earlier. Emitting both put two
    overloads on one name: nanobind tries them in order, the typed
    one matches every same-type comparison, and the parts one never
    ran. They agreed only because the members ARE the parts."""
    fields = wire_fields(cls)
    if not fields and cls.decl.shown:
        # A value that declares no FIELDS and one thing worth showing.
        # The repr then has no name to print, so it prints the value
        # alone - `ValidPathInfo('/nix/store/...')`. Weaker than a
        # named field, and it is what the declaration carries.
        return [f'{INDENT * 2}.def("__repr__", [](nb::handle h) {{',
                f'{INDENT * 3}return nb::str("{cls.name}({{!r}})").format(',
                f'{INDENT * 4}h.attr("{cls.decl.shown}")());',
                f"{INDENT * 2}}})"]
    if not fields:
        return []
    spec = ", ".join(f"{name}={{!r}}" for name, _, _ in fields)
    reads = ", ".join(read for _, _, read in fields)
    hashed = ", ".join(
        f"{NAMESPACE}::as_tuple({read})" if wire.startswith("list[") else read
        for _, wire, read in fields)
    out = []
    if equality and cls.decl.compare != "cxx":
        # Equal when the SAME CLASS carries the same declared parts.
        #
        # `b.type().is(a.type())` rather than isinstance: a subclass
        # of a value type would carry parts this one does not compare,
        # so saying "equal" would be a claim the parts do not support.
        #
        # NotImplemented rather than False for another type, which is
        # what lets the other side answer - and what `nb::is_operator`
        # already does for an overload that does not match.
        out += [
            f'{INDENT * 2}.def("__eq__", [](nb::handle a, nb::handle b)',
            f"{INDENT * 3} -> nb::object {{",
            f"{INDENT * 3}if (!b.type().is(a.type()))",
            f"{INDENT * 4}return nb::not_implemented();",
            f'{INDENT * 3}return nb::cast(a.attr("_parts")()'
            f'.equal(b.attr("_parts")()));',
            f"{INDENT * 2}}}, nb::is_operator())",
        ]
    return [
        *out,
        f'{INDENT * 2}.def("__repr__", [](nb::handle h) {{',
        f'{INDENT * 3}return nb::str("{cls.name}({spec})").format(',
        f"{INDENT * 4}{reads});",
        f"{INDENT * 2}}})",
        f'{INDENT * 2}.def("__hash__", [](nb::handle h) {{',
        f"{INDENT * 3}return nb::hash(nb::make_tuple({hashed}));",
        f"{INDENT * 2}}})",
    ]


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
    if cls.ctor.cxx_body:
        # Placement new, because `__init__` is handed storage rather
        # than asked for an object. One Python signature over several
        # C++ constructors needs this: nb::init picks by C++ type at
        # compile time, and which constructor to call is a decision
        # about a VALUE - MockDerivedPath is opaque when it carries no
        # output name and built when it does.
        obj = _self(cls)
        args, opening = _signature(cls, cls.ctor, known)
        names = "".join(
            f', "{pr.name}"_a'
            + (f" = {_default(pr, known)}" if pr.default is not None else "")
            for pr in cls.ctor.params)
        body = [f"{INDENT * 4}{ln}".rstrip()
                for ln in cls.ctor.cxx_body.strip().splitlines()]
        head = (f'{INDENT * 2}.def("__init__", '
                f"[]({_held(cls)} *{obj}{args}) {{")
        tail = f"{INDENT * 2}}}{names}"
        if not cls.ctor.doc:
            return [head, *opening, *body, tail + ")"]
        doc = " ".join(cls.ctor.doc.split()).replace("\\", "\\\\").replace('"', r'\"')
        return [head, *opening, *body, tail + ",",
                f'{INDENT * 3}     "{doc}")']
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

    All of it from `@wire_value` and `@binding`, so a value prints and
    compares as the thing it IS without a second declaration saying so.

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
    if decl.wire == "value":
        # A value COPIES. Without these, copy.copy falls through to
        # pickle, which a bound C++ type cannot do - so a caller gets
        # TypeError rather than a copy.
        #
        # A bound value is immutable, so a deep copy IS a copy.
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

    Real types, every one. This is what the Cython route could not do:
    a pxd cannot declare a std::optional, a std::set or a member with
    no default constructor, so it turned a store path into its base
    name, absence into an empty string and a set into a vector of
    strings - and Python then held nine slots that had each been
    printed and re-parsed on the way.

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
    """What a RECORD owes Python beyond reading its own fields.

    Equality, and only equality. The repr and the hash beside it are
    what EVERY wire value owes and come from `_identity_semantics`,
    which reads the same declared parts for a record and for a
    constructed value alike.

    `__ne__` is not here and does not need to be. Python fills the
    slot as soon as `__eq__` exists."""
    held = _held(cls)
    return [
        f'{INDENT * 2}.def("__eq__", [](const {held} &a, const {held} &b)',
        f"{INDENT * 3} {{ return a == b; }}, nb::is_operator())",
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
    layer asks for."""
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


# What `_from_parts` is for, in one sentence a caller can read.
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
    declaration repeats the other's spelling.

    The extras come from the FACTORY, not from the `__init__` beside
    it, because the factory is what runs. `open_store` carries
    `@blocks` - opening a daemon store connects, and a local one may
    create its database - and it carries the default `uri="auto"`.
    Reading them off the constructor instead dropped both: the call
    held the GIL for the length of an open, and `Store()` raised
    where the declaration said it should work."""
    if cls.ctor is None:
        return []
    made = next((f for f in functions if f.name == cls.decl.built_by), None)
    if made is None:
        # A factory this declaration does not carry. The class is
        # still bound; it just offers no way in, which is the honest
        # answer until the factory is declared too.
        return []
    line = f"{INDENT * 2}.def(nb::new_(&{made.binds}){_extras(cls, made, known)}"
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


# What `_parts` is for, in one sentence a caller can read.
PARTS_DOC = ("Wire-serialization helper (private): one value per "
             "_wire_fields entry, in order.")


# A declared field's WIRE type, and the C++ that carries it. The wire
# spells types the way the manifest does - `str`, `int`, `str?`, or
# another declared class - because that is the vocabulary the message
# shape is written in, not C++'s.
FIELD_CXX = {"str": "std::string", "int": "std::int64_t", "bool": "bool"}


def _field_cxx(wire: str, known: dict[str, Class] | None = None) -> str:
    """One declared field's type, as C++ holds it."""
    known = known or {}
    if wire.endswith("?"):
        return f"std::optional<{_field_cxx(wire[:-1], known)}>"
    if wire in FIELD_CXX:
        return FIELD_CXX[wire]
    if wire in known:
        return _bare(known[wire])
    raise TypeError(
        f"'{wire}' has no C++ spelling as a field. Add it to "
        f"nbemit.FIELD_CXX, or declare the class it names.")


def _from_parts(cls: Class, known: dict[str, Class] | None = None
                ) -> list[str]:
    """`_from_parts`, for a value nothing constructs.

    A wire value has to be rebuildable from its parts: it crosses as a
    message and the far side has only those. Where a public
    constructor takes exactly the parts, `markers` names the class and
    there is nothing to write. Where there is no public constructor,
    the C++ one still takes them - MockStorePath refuses
    `MockStorePath(...)` in Python and fake_library::StorePath parses
    a base name happily - so this calls it directly, under the private
    name the wire layer asks for."""
    fields = wire_fields(cls)
    if not fields:
        return []
    types = [_field_cxx(wire, known) for _, wire, _ in fields]
    args = ", ".join(f"{t} {n}" for (n, _, _), t in zip(fields, types,
                                                        strict=True))
    names = ", ".join(n for n, _, _ in fields)
    keywords = "".join(f', "{n}"_a' for n, _, _ in fields)
    return [f'{INDENT * 2}.def_static("_from_parts", []({args}) {{',
            f"{INDENT * 3}return {_held(cls)}({names});",
            f'{INDENT * 2}}}{keywords}, "{FROM_PARTS_DOC}")']


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


def _overridable(cls: Class) -> tuple[Method, ...]:
    """The methods a Python subclass may override.

    Empty for almost every class. A binding over a C++ hierarchy that
    Python is meant to EXTEND is the exception, and it needs a
    trampoline - see `trampoline`."""
    return tuple(m for m in cls.methods if m.virtual)


def trampoline(cls: Class, known: dict[str, Class] | None = None) -> str:
    """The C++ subclass that forwards a virtual back to Python.

    Without one, a Python override is invisible: a free function
    taking the base calls the C++ implementation, and the class that
    overrode `get_uri` in Python is never asked.

    nanobind ships the whole mechanism as two macros, so this emits
    six lines. Reaching Python by hand takes forty: a PyStore with
    PyGILState_Ensure, PyObject_CallMethod, a UTF-8 encode and the
    reference counting around all of it.

    NB_TRAMPOLINE takes the arity because it sizes a small table of
    cached lookups. Derived, like everything else here: it is the
    number of methods the declaration marked `@virtual`.

    `NB_OVERRIDE_PURE` for a method with no implementation behind it.
    The plain macro falls back to the base's own implementation when
    no Python subclass overrides, and a pure virtual has none - which
    is a LINK error rather than a compile one, so it surfaces on
    import as an undefined symbol."""
    methods = _overridable(cls)
    if not methods:
        return ""
    held = _held(cls)
    out = [f"namespace {NAMESPACE} {{", "",
           f"/** Forwards {cls.name}'s virtuals to a Python override. */",
           f"struct Py{cls.name} : public {held}",
           "{",
           f"{INDENT}NB_TRAMPOLINE({held}, {len(methods)});", ""]
    for m in methods:
        spelled = m.cxx_name or m.name
        ret, _ = _cxx(m.ret, known) if m.ret is not None else ("void", None)
        args = ", ".join(f"{_param(t, known)[0]} {n}" for n, t in m.params)
        names = "".join(f", {n}" for n, _ in m.params)
        out += [f"{INDENT}{ret} {spelled}({args}) const override",
                f"{INDENT}{{",
                f"{INDENT * 2}NB_OVERRIDE"
                f"{'_PURE' if m.pure else ''}({spelled}{names});",
                f"{INDENT}}}", ""]
    return "\n".join([*out, "};", "", f"}}  // namespace {NAMESPACE}", ""])


def markers(cls: Class) -> list[str]:
    """The facts every layer above reads off the compiled class.

    Every one comes off the declaration, and the generated layer -
    the async wrappers, the protocols, the RPC stubs, the type stubs -
    reads them off the compiled class rather than being told twice.

    `_binds` is NOT here, and its absence is information. It named the
    pxd declaration a pyx class bound, so it was a fact about Cython
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
    if decl.abstract:
        # A base that is GENERATED but never constructed. It still
        # gets an async wrapper and a wire identity - a caller holds
        # the base far more often than a leaf.
        out.append(f'{INDENT}cls.attr("_abstract") = true;')
    if cls.is_value:
        out.append(f'{INDENT}cls.attr("_produced") = true;')
    elif decl.built_by:
        # Which free function makes one. A handle rather than a
        # value: a value is produced and has no factory to name.
        out.append(f'{INDENT}cls.attr("_ctor_from") = "{decl.built_by}";')
    if decl.tree:
        # A LITERAL, parsed once at import.
        #
        # `_tree` is data: nested dicts, lists and strings that the
        # RPC layer reads so no layer above the declaration knows what
        # this type is or which of its methods do what. Building that
        # structure with nb::dict and nb::list would take a dozen
        # temporaries and would render as something a reader has to
        # reassemble in their head.
        #
        # The declaration wrote a Python literal. This carries it
        # across as one and lets Python parse it, which is exact by
        # construction: `ast.literal_eval` is the inverse of the
        # `repr` that produced the text, and it evaluates nothing
        # else.
        out.append(f'{INDENT}cls.attr("_tree") = nb::module_::import_("ast")')
        out.append(f'{INDENT * 2}.attr("literal_eval")({json.dumps(decl.tree)});')
    fields = wire_fields(cls)
    if fields:
        pairs = ", ".join(f'nb::make_tuple("{n}", "{t}")'
                          for n, t, _ in fields)
        out.append(f'{INDENT}cls.attr("_wire_fields") = '
                   f"nb::make_tuple({pairs});")
        if not cls.is_value and cls.ctor is not None:
            # The other half of the round trip, and for a CONSTRUCTED
            # value it is the class.
            #
            # `_from_parts` takes one value per _wire_fields entry, in
            # order, and hands back the value they make. That is
            # exactly what the constructor takes, so naming the class
            # is the whole helper and it cannot drift from the
            # constructor.
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
    known = known or {}
    # The C++ base, and the trampoline where the declaration named a
    # virtual. nanobind takes both as template arguments and does the
    # rest: a method declared once on the base is reachable from every
    # leaf, and a Python override becomes visible to C++.
    holds = [held]
    if decl.base:
        holds.append(_held(known[decl.base]) if decl.base in known
                     else decl.base)
    if _overridable(cls):
        holds.append(f"{NAMESPACE}::Py{cls.name}")
    # A WIRE VALUE is final, and that is a contract rather than a
    # preference. Such a class crosses as its declared parts, so a
    # subclass carrying state no part reads would arrive on the far
    # side silently missing it - and `__eq__` and `__hash__` are
    # declared over those same parts, so a subclass would compare and
    # hash equal to a base that is not the same object at all.
    #
    # It also closes the one thing that made `__eq__` subtle. A typed
    # `const T &` comparison accepts a derived instance, so the
    # same-class rule the declaration states was enforced only by a
    # cast that happens to fail. Nothing can derive from it now.
    #
    # A PROXY is not final: MockStore exists to be subclassed, which
    # is what its trampoline is for.
    final = ", nb::is_final()" if decl.wire == "value" else ""
    lines = [f"static void bind_{cls.name.lower()}(nb::module_ &m) {{",
             f'{INDENT}auto cls = nb::class_<{", ".join(holds)}>'
             f'(m, "{cls.name}"{final})']
    if cls.is_value:
        # A RECORD: the emitter declared the struct, so every accessor
        # is a member and the whole binding is derived from the field
        # list.
        body = _record_ctor(cls, known)
        obj = _self(cls)
        # METHODS, not `def_ro` properties. The declaration writes
        # `def path(self) -> StorePath`, so a caller writes
        # `info.path()`. A property would read better and would be a
        # DIFFERENT surface, which is not a choice an emitter makes
        # on its own.
        body += [f'{INDENT * 2}.def("{name}", [](const {held} &{obj}) '
                 f"{{ return {obj}.{name}; }})"
                 for name, _ in record_fields(cls, known)]
        body += _record_semantics(cls, known)
        # ...which owns `__eq__` for a record, so this contributes
        # the repr and the hash alone.
        body += _identity_semantics(cls, known, equality=False)
        body += _round_trip(cls)
        body += _value_semantics(cls)
        if body:
            body[-1] += ";"
        return "\n".join([*lines, *body, *markers(cls), "}"]) + "\n"
    # No nb::init when something else builds one: there is no
    # constructor to call. A FACTORY takes its place where the
    # declaration names one. An ABSTRACT class offers neither: a
    # caller holds one all the time and constructs one never.
    if decl.abstract:
        # No declared constructor, and still a way in - for a SUBCLASS
        # only. A Python class deriving from this one is instantiated
        # as the trampoline, and nanobind needs an `__init__` to reach
        # it; `@abstract` says a caller may not reach it directly.
        #
        # `nb::init<>()` alone gives BOTH, because the held type is
        # abstract in C++ too: `std::is_constructible_v<Type>` is
        # false, so nanobind's own init always builds the trampoline
        # and `MockStore()` succeeds. It then raises from the first
        # call, which is a worse place to learn about it.
        #
        # `pointer_and_handle` and `nb_inst_python_derived` are how
        # nanobind's `init` asks the same question (nb_class.h:393) -
        # the storage to construct into, and whether the Python type
        # being built is a subclass of the bound one.
        body = ([f'{INDENT * 2}.def("__init__", '
                 f"[](nb::pointer_and_handle<{_held(cls)}> v) {{",
                 f"{INDENT * 3}if (!nb::detail::nb_inst_python_derived("
                 f"v.h.ptr()))",
                 f'{INDENT * 4}throw nb::type_error("{cls.name} is '
                 f'abstract: derive from it, or open one through a '
                 f'factory.");',
                 f"{INDENT * 3}new ((void *) v.p) "
                 f"{NAMESPACE}::Py{cls.name}();",
                 f"{INDENT * 2}}})"]
                if _overridable(cls) else [])
    elif decl.built_by:
        body = _factory(cls, functions, known)
    else:
        body = _ctor(cls, known)
    for m in cls.methods:
        body += _accessor(cls, m) if m.prop else _method(cls, m, known)
    if decl.wire == "value":
        body += _identity_semantics(cls, known)
        body += _round_trip(cls)
        if cls.ctor is None:
            body += _from_parts(cls, known)
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
    if not (fn.binds or fn.cxx_body):
        raise TypeError(
            f"{fn.name}: a free function names the C++ it binds, or "
            f'carries it. Use @binds("cxx_name") or @cxx_body(...).')
    extras = []
    if fn.blocks and not fn.instant:
        extras.append("nb::call_guard<nb::gil_scoped_release>()")
    for pr in fn.params:
        arg = f'"{pr.name}"_a'
        if pr.default is not None:
            arg += f" = {_default(pr, known)}"
        extras.append(arg)
    tail = "".join(f", {x}" for x in extras)
    if not fn.cxx_body:
        return [f'{INDENT}m.def("{fn.name}", &{fn.binds}{tail});']
    # A body, for a function whose C++ is assembled rather than named.
    # `gc_stats` reads five counters out of gc.h and hands back one
    # dict; there is no upstream function with that shape to point at.
    args = ", ".join(f"{_param(pr.type, known)[0]} {pr.name}"
                     for pr in fn.params)
    body = [f"{INDENT * 2}{ln}".rstrip()
            for ln in fn.cxx_body.strip().splitlines()]
    return [f'{INDENT}m.def("{fn.name}", []({args}) {{', *body,
            f"{INDENT}}}{tail});"]


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
# difference from a set flattened into a vector of strings.
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
    slots - so it has no
    `nb::class_` to be until the declaration names the type it came
    from.

    Skipping is honest here rather than quiet, because
    `generate.emit_module` prints what it left out beside what it
    wrote."""
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
    for cls in classes:
        head += trampoline(cls, known).splitlines()
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

    ONCE for the module, and it runs for any binding in it. The
    Cython route named `except +translate_nix_error` on every method
    in the pxd, so the hook ran per call; this registers it in one
    place. The C++ is shared rather than emitted:
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
                // Sets the Python error from inside catch(...),
                // which is where the exception is still live.
                {fn.binds}();
            }}
        }});
}}
"""


def census(cls: Class) -> dict[str, int]:
    """How much of this class the declaration derived, and how much a
    person wrote.

    Printed on every build, because a hatch nobody measures becomes
    the place the real code lives. The ratio is the honest measure of
    a binding: StorePath derives whole and hatches nothing;
    ValidPathInfo joins a store directory to a path, which is a
    decision rather than a binding, and it says so with seven
    bodies."""
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
