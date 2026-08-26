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

from cythonix_idl.declare import Field
from cythonix_idl.read import Class, Method, Type

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


def _bare(cls: Class) -> str:
    """The C++ type behind a declared class, or a refusal."""
    if cls.is_words:
        # A vocabulary. The member IS the string a Nix parser takes,
        # so it crosses as one - a fact about the words rather than
        # about either binding.
        return "std::string"
    if not cls.decl.cxx:
        raise TypeError(
            f"'{cls.name}' has no C++ type behind it. Only a class with "
            f"@binding(cxx=...) can cross as one.")
    return cls.decl.cxx


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
        spelled = _bare(known[inner])
        return spelled, "string" if spelled == "std::string" else None
    if inner in CXX_PYTHON:
        return CXX_PYTHON[inner]
    if t.cxx is None or t.cxx.spelling not in CXX_PARAM:
        raise TypeError(
            f"'{t.python}' has no C++ spelling. A bound class names types "
            f"through an Annotated alias in declare.py.")
    spelled, caster = CXX_PARAM[t.cxx.spelling]
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
        # copying. `nb::bytes` is already a handle and says so itself.
        if spelled.startswith(("nb::", "const ")):
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
        if not other.decl.cxx:
            raise TypeError(
                f"'{t.python}' has no C++ type behind it. Only a class with "
                f"@binding(cxx=...) can cross as one.")
        return f"const {other.decl.cxx} &", None
    if t.cxx is None or t.cxx.spelling not in CXX_PARAM:
        raise TypeError(
            f"'{t.python}' has no C++ parameter spelling. A bound class "
            f"names types through an Annotated alias in declare.py.")
    return CXX_PARAM[t.cxx.spelling]


def includes(cls: Class, known: dict[str, Class] | None = None) -> list[str]:
    """Exactly the headers this binding needs, and no others.

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

    for m in cls.methods:
        for _, t in m.params:
            note(t)
        note(m.ret)
    if cls.ctor is not None:
        for _, t in cls.ctor.params:
            note(t)

    out = ["#include <nanobind/nanobind.h>"]
    out += [f"#include <nanobind/stl/{c}.h>" for c in sorted(casters)]
    if cls.decl.wire == "value" and cls.decl.text:
        # std::hash lives in <functional>, and the value hash uses it.
        out.append("#include <functional>")
    # The class's header, then whatever the bodies reach past it.
    # Sorted and de-duplicated, because two methods needing one
    # header is normal and the order of a declaration's methods is
    # not an order for includes.
    wanted = {cls.decl.header} | {h for m in cls.methods for h in m.headers}
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
    inner = pr.type.python.removesuffix("| None").strip()
    if value == "None" and inner.startswith("list["):
        # SPELLED, not `{}`. A braced initialiser has no type to
        # deduce, so nanobind takes the default and silently drops
        # the parameter's NAME with it - the signature comes out as
        # `arg4` and the keyword stops working.
        spelled, _ = _cxx(pr.type, known)
        return f"{spelled}{{}}"
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
    if m.cxx_body:
        # A method the declaration could not derive, carried verbatim.
        obj = _self(cls)
        args = "".join(f", {_param(pr.type, known)[0]} {pr.name}"
                       for pr in m.params)
        head = (f'{INDENT * 2}.def("{m.name}", '
                f"[]({cls.decl.cxx} &{obj}{args}) {{")
        body = [f"{INDENT * 4}{ln}".rstrip()
                for ln in m.cxx_body.strip().splitlines()]
        return [head, *body, f"{INDENT * 2}}}{_extras(cls, m, known)})"]
    spelled = m.cxx_name or m.name
    return [f'{INDENT * 2}.def("{m.name}", &{cls.decl.cxx}::{spelled}'
            f"{_extras(cls, m, known)})"]


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
    ref = f"const {decl.cxx} &{obj}"
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
                f"{{ return {decl.cxx}({obj}); }})",
                f'{INDENT * 2}.def("__deepcopy__", []({ref}, nb::dict) '
                f"{{ return {decl.cxx}({obj}); }}, \"memo\"_a)"]

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


def bind_function(cls: Class, known: dict[str, Class] | None = None) -> str:
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
    # No nb::init when something else builds one: offering a
    # constructor would advertise a way in that does not exist.
    body = [] if decl.built_by else _ctor(cls, known)
    for m in cls.methods:
        body += _accessor(cls, m) if m.prop else _method(cls, m, known)
    body += _value_semantics(cls)
    for source in decl.custom.values():
        body += [f"{INDENT * 2}{line}".rstrip()
                 for line in source.splitlines()]
    if body:
        body[-1] += ";"
    return "\n".join([*lines, *body, "}"]) + "\n"


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
            arg += f" = {CXX_DEFAULT.get(pr.default, pr.default)}"
        extras.append(arg)
    tail = "".join(f", {x}" for x in extras)
    return [f'{INDENT}m.def("{fn.name}", &{fn.binds}{tail});']


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
"""


def _crosses_container(cls: Class) -> bool:
    """Whether any declared type here is a list of bound values."""
    for m in cls.methods:
        spelled = [t.python for _, t in m.params]
        if m.ret is not None:
            spelled.append(m.ret.python)
        for one in spelled:
            inner = one.removesuffix("| None").strip()
            if inner.startswith("list[") and inner[5:-1][:1].isupper():
                return True
    return False


def module(cls: Class, known: dict[str, Class] | None = None) -> str:
    """One translation unit: the includes, then the bind function."""
    head = [*includes(cls, known), "", "namespace nb = nanobind;",
            "using namespace nb::literals;", ""]
    if _crosses_container(cls):
        head += [*CONTAINERS.strip().splitlines(), ""]
    return "\n".join(head) + "\n" + bind_function(cls, known)


def extension(cls: Class, name: str,
              known: dict[str, Class] | None = None) -> str:
    """One whole extension module: includes, bindings, entry point.

    `module` stops at the `bind_<name>` function because that is the
    seam a project with a hand-written NB_MODULE needs. This goes the
    last step and writes the NB_MODULE too, which is what a module
    with nothing hand-written about it requires.

    The two are one line apart on purpose. A project adopting this
    gradually keeps its own entry point and calls the generated bind
    function; a project that has finished takes this."""
    return "\n".join([
        '#include "cythonix_bindings/_cpp/errors.hpp"',
        module(cls, known),
        TRANSLATOR,
        f"NB_MODULE({name}, m) {{",
        f"{INDENT}register_nix_errors();",
        f"{INDENT}bind_{cls.name.lower()}(m);",
        "}",
        "",
    ])


# Every declared method can raise a nix exception, so every module
# has to turn one into the right Python class. The two backends do
# that at different granularities and from one piece of C++.
#
# Cython writes `except +translate_nix_error` on every method in the
# pxd, so the hook runs per call. nanobind registers a translator
# ONCE for the module, and it runs for any binding in it. Same fact,
# two spellings, and neither is in the declaration - a nix binding
# translates nix errors, which is not a choice a declaration makes.
#
# The ladder itself is shared rather than emitted twice. `errors.hpp`
# maps nix::BadStorePath onto cythonix_bindings.errors.BadStorePath
# and strips libstore's terminal escapes; nothing about that is
# Cython's, and writing it a second time in this file would be one
# fact in two places with no gate between them.
TRANSLATOR = """
static void register_nix_errors() {
    nb::register_exception_translator(
        [](const std::exception_ptr &p, void *) {
            try {
                std::rethrow_exception(p);
            } catch (...) {
                // Sets the Python error from inside catch(...), which
                // is the same position Cython calls it from.
                cythonix::translate_nix_error();
            }
        });
}
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
