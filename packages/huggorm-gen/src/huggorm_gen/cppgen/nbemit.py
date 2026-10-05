"""
Declaration -> nanobind C++.

The one backend, and the reason the declaration exists. It reads a
class off the typed model (`ir.ClassModel`) and writes one C++
function. The declaration is not C++
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

from collections.abc import Iterator, Sequence

from huggorm_gen import cxx, ir
from huggorm_gen.cxx import NAMESPACE
from huggorm_gen.cxx import held as _held

INDENT = "    "

# Comparison dunders and the C++ operator each one binds. Every entry
# is emitted with nb::is_operator(); see the module docstring.
# A Python default, spelled for C++. Only where the two differ: a
# number or a string literal already reads the same in both.
COMPARISONS = (("__eq__", "==", "value"), ("__lt__", "<", "order"),
               ("__le__", "<=", "order"), ("__gt__", ">", "order"),
               ("__ge__", ">=", "order"))


def _doc(text: str) -> str:
    """One docstring, as the C++ string literal that carries it.

    Flattened to a line, however the declaration wrapped it: a C++
    literal has no continuation and gluing two is noise. `help()`
    rewraps anyway.

    Empty for a declaration that wrote none, and the caller emits
    nothing rather than an empty literal - a `__doc__` of None says
    "undocumented" where "" says "documented as nothing"."""
    if not text or not text.strip():
        return ""
    return " ".join(text.split()).replace("\\", "\\\\").replace('"', r'\"')


# The lambda's parameter name for the bound object, in every emitted
# body of every class.
#
# It used to be the class's initials - StorePath `sp`, PathInfo `pi`,
# DerivedPathBuilt `dpb` - which is consistent and is a second thing to
# know per class. One name is charm at four classes and a lookup at
# forty, and a declaration writes its bodies against it: `self.narHash`
# reads as the Python the file already is, and compiles as the C++ it
# becomes.
#
# Invisible to a caller either way. It names a C++ lambda argument, and
# `self` is not a C++ keyword.
SELF = "self"


def _self(cls: cxx.Declared) -> str:
    """The lambda's parameter name for the bound object."""
    return SELF




# What `to_bytes` and `from_bytes` convert, and nothing else does.
BYTES_SPELLINGS = ("nb::bytes", "std::vector<nb::bytes>")



def _sites(classes: Sequence[ir.ClassModel],
           functions: Sequence[ir.FunctionModel] = (),
           ) -> Iterator[tuple[ir.ParamModel | None, ir.TypeRef | None]]:
    """Every place this translation unit names a declared type.

    One walk, because two things read the same sites and a second
    walk would be the same list written twice: the includes need
    every type's caster, and the conversions need every union.

    A parameter comes with its `ParamModel`, because a container that
    reads None arrives as an optional and only the parameter says so.
    A return yields None in its place.

    A free function belongs to no class, so its types reach a caller
    of this only from the last loop - and `open_store` is the one
    that brings <nanobind/stl/shared_ptr.h> in.
    """
    for cls in classes:
        for m in cls.bound:
            for pr in m.params:
                yield pr, pr.type
            yield None, m.returns
        if cls.init is not None:
            for pr in cls.init.params:
                yield pr, pr.type
        if cls.from_parts is not None:
            yield None, cls.from_parts.returns
    for fn in functions:
        for pr in fn.params:
            yield pr, pr.type
        yield None, fn.returns


# What a `Cxx` body spells, and the standard header that defines it.
#
# A body is TEXT the declaration carries, so which headers it needs is
# DERIVED here rather than carried by a hand-written one on the
# emitted file's behalf. That was the arrangement huggorm#90 found:
# `eval.hpp` held a `<stdexcept>` it never used, for bodies that throw
# `std::invalid_argument` 54 times - a fact about generated code,
# living in a file a person maintains.
#
# Only what a caster does NOT already bring. `<string>` and `<vector>`
# arrive with `nanobind/stl/string.h` and its kind, so listing them
# here would add a line that is already there.
#
# It GROWS when a body spells something new. That is the point of a
# table over a guess: a body reaching for `std::filesystem` gets its
# header the day somebody adds the row, and until then the build fails
# loudly at compile time rather than quietly at run time.
BODY_HEADERS = {
    "std::invalid_argument": "stdexcept",
    "std::runtime_error": "stdexcept",
    "std::logic_error": "stdexcept",
    "std::out_of_range": "stdexcept",
    "std::domain_error": "stdexcept",
    "std::int64_t": "cstdint",
    "std::uint64_t": "cstdint",
    "std::int32_t": "cstdint",
    "std::uint32_t": "cstdint",
    "std::uintptr_t": "cstdint",
    "std::size_t": "cstddef",
    "std::sort": "algorithm",
    "std::move": "utility",
    "std::exchange": "utility",
    "std::forward": "utility",
}


def _bodies(classes: Sequence[ir.ClassModel],
            functions: Sequence[ir.FunctionModel] = ()) -> Iterator[str]:
    """Every piece of hand-written C++ this translation unit carries.

    One walk, like `_sites`, and for the same reason. A body reaches
    the emitted file from five places and a header it needs is a
    header it needs from any of them."""
    for cls in classes:
        for m in cls.bound:
            yield m.cxx_body
        if cls.init is not None:
            yield cls.init.cxx_body
        if cls.from_parts is not None:
            yield cls.from_parts.cxx_body
        yield from cls.decl.custom.values()
    for fn in functions:
        yield fn.cxx_body


def waits(cls: ir.ClassModel,
          m: ir.MethodModel | ir.FunctionModel) -> bool:
    """Whether this call can block, and so needs the GIL released.

    The class states the general case and a method overrides it in
    either direction - `@blocks` on a class that mostly does not,
    `@instant` on one that mostly does. Releasing the GIL is not free:
    it costs two thread-state transitions, so doing it around a read
    of a string already in memory is a loss."""
    if m.instant:
        return False
    return cls.decl.blocking or m.blocks



def words_from_word(cls: ir.EnumModel) -> list[str]:
    """A word going TO an enum upstream gives no parser for.

    `nix::BuildMode` has none, and it has no rendering either: it
    crosses Nix's own worker protocol as an integer, so the words
    `normal`, `repair` and `check` are this binding's and not
    upstream's. There is nothing to hand the string to, so the
    mapping is written here.

    ONLY when `parsed_by` is empty. Where upstream has a parser, that
    parser is the one to call - it is the only thing that knows a
    word is behind an experimental feature, and a second mapping
    beside it would be the same list twice.

    A chain rather than a switch, because C++ cannot switch on a
    string. So this direction has NO exhaustiveness check, and it
    does not need one: it names every enumerator, which catches a
    rename, and `as_word` beside it is the switch that catches an
    addition.

    The refusal is spelled the way upstream spells its own -
    `parseHashAlgo` throws UsageError and lists the words it takes -
    so a caller who mistypes gets the same shape of answer wherever
    the word came from."""
    enum = cls.decl.enumerated
    assert enum is not None
    listed = ", ".join(f"'{w.value}'" for w in cls.members)
    out = [f"/** A word, as the {cls.name} libstore holds. */",
           "template <typename T> T from_word(std::string_view word);",
           f"template <> inline {enum.held}",
           f"from_word<{enum.held}>(std::string_view word)",
           "{"]
    for word in cls.members:
        out += [f'{INDENT}if (word == "{word.value}")',
                f"{INDENT * 2}return {enum.enumerator(word.name)};"]
    out += [f'{INDENT}throw nix::UsageError(',
            f'{INDENT * 2}"unknown {cls.name} \'%1%\', expect {listed}",'
            if listed else f'{INDENT * 2}"unknown {cls.name} \'%1%\'",',
            f"{INDENT * 2}word);",
            "}", ""]
    return out






def _wrong_arm(owner: str, hold: str, expected: str) -> str:
    """Nix's own refusal of a value of the wrong type.

    `forceAttrs` says "expected a set but found an integer", as a
    `nix::TypeError`, and so does this: a caller catches one class for
    both. An eval error names its state, so `owner` must have
    `state()`. Nix also prints the value, which this does not."""
    return (f'throw nix::TypeError({owner}.state(), "expected %s but found %s", '
            f"nix::showType({expected}), nix::showType(*{owner}.{hold}));")







def _render(cls: ir.ClassModel, accessor: str) -> str:
    """The Python that renders one of this value's accessors as text.

    Through the PYTHON object, like the repr and the hash beside it,
    and for the same reason those are: the accessor comes back as
    whatever its own binding hands over, so this never has to know
    what it IS. `nb::str` then asks that object to render itself, so a
    part that is a StorePath prints as a StorePath without a line here
    saying so.

    It used to spell the C++ - `self.path`, or `self.to_string()` for
    a method - which held while every accessor bound a member or a
    no-argument call and stopped at the first hatched one:
    `nix::Hash::to_string` takes a format and a flag, so the emitted
    `self.to_string()` did not compile. The declaration's `to_string`
    is the BOUND one, and this is how to reach it."""
    return f'nb::str(h.attr("{accessor}")())'


def _value_semantics(cls: ir.ClassModel) -> list[str]:
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
        out.append(f'{INDENT * 2}.def("__str__", [](nb::handle h) '
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


def _paragraph(doc: str) -> list[str]:
    """The first paragraph of a docstring, as its lines."""
    out: list[str] = []
    for line in doc.strip().splitlines():
        if not line.strip():
            break
        out.append(line.strip())
    return out



def records_include(module: str, package: str) -> str:
    """The name a unit includes a module's records header by.

    Named for the package, as `huggorm_decl/cpp/...` is, because a
    hand-written helper includes one too: `cpp/logging.hpp` holds a
    `huggorm::ErrorInfo` that `decl/path.py` declares."""
    name = f"{module}_records.hpp"
    return f"{package}/{name}" if package else name


def records_header(unit: ir.ModuleModel, package: str,
                   model: ir.Model) -> str | None:
    """The header a module's records are emitted into, or None.

    A HEADER, not the unit, because a record is a C++ type another unit
    can name: `eval`'s `LogRecord` holds `path`'s `ErrorInfo`. A struct
    emitted into its own unit is visible nowhere else (huggorm#103)."""
    values = [c for c in unit.bindable() if c.is_value]
    if not values:
        return None
    emit = Emitter(model, unit)
    others = [m for m in emit.records_named(values, ())
              if m != unit.name]
    structs = emit.records(values)
    # What the fields SPELL, read off the structs as `includes` reads
    # a body: `std::int64_t` needs <cstdint>, and no caster names it.
    text = "\n".join(structs)
    spelled = sorted({h for spelling, h in BODY_HEADERS.items()
                      if spelling in text})
    return "\n".join([
        "#pragma once", "",
        *emit.includes(values, ()),
        *[f"#include <{h}>" for h in spelled],
        *[f'#include "{records_include(m, package)}"' for m in others],
        "",
        *structs,
    ])


def _lists(cls: ir.ClassModel) -> list[str]:
    """The wire parts of this value that cross as lists.

    The PARTS, not the accessors. A value's hash is over what it sends,
    and a list is hashed as a tuple because a list is unhashable -
    which is the one thing `as_tuple` exists for."""
    return [f.name for f in cls.wire_fields
            if f.type.required.origin == "list"]


def _nodes(t: ir.TypeRef | None) -> Iterator[ir.TypeRef]:
    """A declared type and every type it holds, at any depth."""
    if t is None:
        return
    yield t
    for arg in t.args:
        yield from _nodes(arg)



def words_conversion(cls: ir.EnumModel) -> list[str]:
    """One vocabulary coming BACK from C++, as the switch that checks it.

    The direction `parsed_by` does not have. A word going to libstore
    is a string upstream parses; a word coming back is an enumerator,
    and something has to say which word it is.

    That something is a SWITCH WITH NO `default`, and the missing
    `default` is the point rather than a style. With `-Werror=switch`
    the compiler refuses to build the day upstream adds an
    enumerator, and naming `nix::HashAlgorithm::MD5` refuses the day
    upstream removes or renames one. Both were measured before this
    was written (huggorm#70).

    Not upstream's own `printHashAlgo`. It would render correctly and
    check nothing, and the words this repo publishes would drift from
    the enum with no diagnostic. The suite closes the other half: it
    round-trips every declared word through upstream's parser and back
    through this, so our SPELLING is checked against upstream's too.

    The throw past the switch is unreachable and a compiler still
    wants it: every enumerator returns above, and control falling off
    the end of a non-void function is what `-Wreturn-type` is for."""
    enum = cls.decl.enumerated
    assert enum is not None
    out = [f"/** A {cls.name}, as the word Python has. */",
           f"inline std::string as_word({enum.held} value)",
           "{",
           f"{INDENT}switch (value{enum.reach}) {{"]
    for word in cls.members:
        out.append(f"{INDENT}case {enum.enumerator(word.name)}: "
                   f'return "{word.value}";')
    out += [f"{INDENT}}}",
            f'{INDENT}throw nix::Error("unknown {enum.held}");',
            "}", ""]
    return out



def _is_bare(cls: ir.UnionModel) -> bool:
    return cls.decl.variant is not None and cls.decl.variant.bare


def _arm(cls: ir.UnionModel, name: str) -> str:
    """One arm as the arms' variant holds it."""
    return next(a.cxx for a in cls.arms if a.name == name)


def _arms_type(cls: ir.UnionModel) -> str:
    """A union as the std::variant of the arms PYTHON has: what the
    caster casts through, and what `from_arms` takes."""
    return f"std::variant<{', '.join(a.cxx for a in cls.arms)}>"



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



# What `_from_parts` is for, in one sentence a caller can read.
FROM_PARTS_DOC = "Wire-deserialization helper (private, never surfaced)."



def wire_fields(cls: ir.ClassModel) -> list[tuple[str, ir.TypeRef, str]]:
    """What this value is made of, as (name, type, how to read).

    The third element is a Python expression on a handle called `h`,
    which is how one line covers a str, a store path and a list of
    them: the part comes back as whatever its own binding hands over,
    so nothing here knows what a part IS."""
    return [(f.name, f.type, f'h.attr("{f.read}")()')
            for f in cls.wire_fields]


# What `_parts` is for, in one sentence a caller can read.
PARTS_DOC = ("Wire-serialization helper (private): one value per "
             "_wire_fields entry, in order.")


# A declared field's WIRE type, and the C++ that carries it. The wire
# spells types the way a declaration does - `str`, `int`, `str?`, or
# another declared class - because that is the vocabulary the message
# shape is written in, not C++'s.



def _round_trip(cls: ir.ClassModel) -> list[str]:
    """`_parts`, the half of the wire round trip that is a method.

    The other half is `_from_parts`, and it needs no code at all: see
    `markers`.

    Through the PYTHON object, like the repr and the hash beside it.
    A part of any bound type comes back as whatever that type's own
    binding hands over, so this emitter never has to know what a part
    IS - which is what lets one line cover a str, a StorePath and a
    list of them."""
    fields = wire_fields(cls)
    if not fields and not cls.decl.unit:
        return []
    reads = ", ".join(read for _, _, read in fields)
    return [f'{INDENT * 2}.def("_parts", [](nb::handle h) {{',
            f"{INDENT * 3}return nb::make_tuple({reads});",
            f'{INDENT * 2}}}, "{PARTS_DOC}")']


def markers(cls: ir.ClassModel) -> list[str]:
    """The class attributes the runtime reads off a compiled object.

    `_wire`, which `unwrap_arg` reads to copy a value rather than hand
    over a handle, and `_from_parts` for a constructed value. Every
    other fact about a class reaches the layers above through the
    model, not through the compiled class."""
    decl = cls.decl
    # The EFFECTIVE value, not the declared one. "proxy" is the safe
    # default on both sides - stateful until a declaration proves
    # otherwise - and writing it out means a reader of the compiled
    # class is told rather than left to know the default.
    out = [f'{INDENT}cls.attr("_wire") = "{decl.wire or "proxy"}";']
    fields = wire_fields(cls)
    if (fields or cls.decl.unit) and not cls.is_value and cls.init is not None:
        # The other half of the round trip, and for a CONSTRUCTED value
        # it is the class.
        #
        # `_from_parts` takes one value per wire field, in order, and
        # hands back the value they make. That is exactly what the
        # constructor takes, so naming the class is the whole helper and
        # it cannot drift from the constructor.
        #
        # A PRODUCED value has no public constructor to name: its
        # `__init__` raises, and `_from_parts` is bound beside it as a
        # static method.
        #
        # Naming the class CLAIMS the constructor takes the wire fields,
        # in order, which the model checks.
        out.append(f'{INDENT}cls.attr("_from_parts") = cls;')
    return out



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

/**
 * A libstore map as the `std::map<std::string, V>` a caller reads.
 * libstore keys many maps with a transparent `std::less<>`, such as
 * `StringPairs`, and nanobind casts only the plain comparator.
 */
template <typename T>
inline std::map<std::string, typename T::mapped_type> as_map(const T & items)
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


# The two directions between a string a value holds and the bytes a
# caller reads. `nb::bytes` has only explicit constructors, so neither
# direction happens on its own. `to_bytes`, not `as_bytes`: <span>
# declares a `std::as_bytes` that overload resolution finds first.
BYTES = """
/** A string as the bytes a caller reads. */
inline nb::bytes to_bytes(std::string_view s)
{
    return nb::bytes(s.data(), s.size());
}

inline std::vector<nb::bytes> to_bytes(const std::vector<std::string> & items)
{
    std::vector<nb::bytes> out;
    out.reserve(items.size());
    for (auto & s : items)
        out.push_back(to_bytes(s));
    return out;
}

/** The mirror: bytes back into the string a value holds, unread. */
inline std::string from_bytes(const nb::bytes & b)
{
    return std::string(b.c_str(), b.size());
}

inline std::vector<std::string> from_bytes(const std::vector<nb::bytes> & items)
{
    std::vector<std::string> out;
    out.reserve(items.size());
    for (auto & b : items)
        out.push_back(from_bytes(b));
    return out;
}
"""


# What a declared class leaf is, as opposed to a builtin, an opaque
# object or a module type.
DECLARED = ("enum", "union", "error", "value", "proxy")


def _crosses_container(classes: Sequence[ir.ClassModel]) -> bool:
    """Whether any declared type here is a list of bound values."""
    for cls in classes:
        for m in cls.bound:
            declared = [pr.type for pr in m.params]
            if m.returns is not None:
                declared.append(m.returns)
            for one in declared:
                for node in _nodes(one):
                    if (node.origin == "list"
                            and node.args[0].kind in DECLARED
                            and not node.args[0].origin):
                        return True
                    # Every `dict` return goes through `as_map`.
                    if node.origin == "dict":
                        return True
    return False


class Emitter:
    """The binding C++ for classes that may name each other.

    `unit` is the module being emitted. A name it cannot see is
    refused, never resolved from the whole `model`. The producers are
    the whole model's: what makes a `PathInfo` lives in another
    module."""

    def __init__(self, model: ir.Model, unit: ir.ModuleModel) -> None:
        self.model = model
        self.unit = unit

    def _produced_ctor(self, cls: ir.ClassModel,
                       because: str = "") -> list[str]:
        """The `__init__` of a class nothing constructs.

        `because` is the sentence, for a class that is unconstructible
        for another reason - an abstract base is the other one.

        It raises, and the message names what DOES make one. Without it
        nanobind answers `TypeError: PathInfo: no constructor defined!`,
        which is true and tells a caller nothing about where to look.
        The calls are read off the return types, so none goes stale."""
        calls = list(self.model.producers.get(cls.name, ()))
        named = (" or ".join([", ".join(calls[:-1]), calls[-1]])
                 if len(calls) > 1 else "".join(calls))
        said = because or (f"objects come from {named}, not from a "
                           f"constructor" if named
                           else "objects come from another call, not from "
                                "a constructor")
        return [
            f'{INDENT * 2}.def("__init__", []({_held(cls)} *) {{',
            f"{INDENT * 3}throw nb::type_error(",
            f'{INDENT * 4}"{cls.name} {said}");',
            f"{INDENT * 2}}})",
        ]

    def includes(self, classes: Sequence[ir.ClassModel],
                 functions: Sequence[ir.FunctionModel],
                 errors: Sequence[str] = ()) -> list[str]:
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

        def note(t: ir.TypeRef | None) -> None:
            if t is None:
                return
            if t.caster:
                casters.add(t.caster)
            # A container or an optional needs what it HOLDS cast too:
            # `list[StorePath]` needs <vector>, and a `list[str]` needs
            # <string> beneath it. Every argument, so no container kind is
            # left out: a union held in a map needs <variant> as surely as
            # a bare one.
            for arg in t.args:
                note(arg)
            # ...and a UNION's arms, for the same reason: the variant
            # caster casts each arm with that arm's own.
            if not t.origin and t.kind == "union":
                for arm in self.model.unions[t.name].arms:
                    note(arm)

        for pr, t in _sites(classes, functions):
            note(t)
            # A CONTAINER that reads None arrives as a std::optional, so
            # it needs that caster even though no declared type here is
            # optional. `_signature` builds the optional; this is the
            # only place that can know it will. Missed until a module had
            # one and nothing else optional in it - `store` had returns
            # to hide it, `derived_path` had not.
            if pr is not None and pr.absent:
                casters.add("optional")
        # A hook has no signature worth casting, and it still names the
        # header its C++ lives in. `wanted` below is where that lands.

        # What the hand-written BODIES spell, which no signature says.
        # Derived from the text the declaration carries - see
        # BODY_HEADERS for why this is not a header's job.
        body = " ".join(b for b in _bodies(classes, functions) if b)
        standard = {h for spelling, h in BODY_HEADERS.items()
                    if spelling in body}

        out = ["#include <nanobind/nanobind.h>"]
        out += [f"#include <nanobind/stl/{c}.h>" for c in sorted(casters)]
        if any(cls.decl.wire == "value" and cls.decl.text for cls in classes):
            # std::hash lives in <functional>, and the value hash uses it.
            out.append("#include <functional>")
        out += [f"#include <{h}>" for h in sorted(standard)]
        # The headers that declare the types the CATCH CHAIN names. The
        # chain is emitted, so the includes it needs are emitted too - the
        # three of them lived in `cpp/errors.hpp` until now, which is a
        # fact about generated code stated in a hand-written helper
        # (huggorm#90). `decl/errors.py` says which header each class is
        # in, beside the `cxx` that names the class.
        out += [f'#include "{h}"' for h in errors]
        # Each class's header, then whatever the bodies reach past it.
        # Sorted and de-duplicated, because two methods needing one
        # header is normal and the order of a declaration's methods is
        # not an order for includes.
        wanted = {cls.header for cls in classes}
        # A union's own type, which no `@header` names: the alias carries
        # it, because a unit that only PASSES one declares none of its
        # arms and would otherwise include nothing that spells it.
        wanted |= {u.decl.variant.header for u in self._unions_used(
            classes, functions) if u.decl.variant is not None}
        # A vocabulary's enum, for the same reason. `hash.cpp` returns a
        # HashAlgorithm and declares no class from `nix/util/hash.hh`
        # beyond its own - the words live in another declaration file.
        wanted |= {v.header for v in self._vocabularies_used(
            classes, functions)}
        # A class from ANOTHER declaration that a signature names. nanobind
        # casts by type, so a forward declaration is not enough:
        # `registry.cpp` names `nix::Store` only as a parameter.
        wanted |= {other.header for _, t in _sites(classes, functions)
                   if t is not None
                   and (other := self.model.declared(t.leaf)) is not None}
        wanted |= {h for cls in classes for h in cls.decl.headers}
        wanted |= {h for cls in classes for m in cls.bound for h in m.headers}
        wanted |= {h for cls in classes if cls.from_parts is not None
                   for h in cls.from_parts.headers}
        wanted |= {h for fn in functions for h in fn.headers}
        out += [f'#include "{h}"' for h in sorted(wanted - {""})]
        return out

    def _extras(self, wait: bool, params: Sequence[ir.ParamModel]) -> str:
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
        if wait:
            out.append("nb::call_guard<nb::gil_scoped_release>()")
        for pr in params:
            arg = f'"{pr.name}"_a'
            if pr.cxx_default:
                arg += f" = {pr.cxx_default}"
            out.append(arg)
        return "".join(f", {x}" for x in out)

    def _derived(self, cls: ir.ClassModel,
                 m: ir.MethodModel) -> list[str] | None:
        """The body of a method the emitter can write itself, or None.

        Three mechanical things a HANDLE forces, and each of them was a
        a verbatim body before this existed:

        - the CALL goes through the handle - `v.get()->type_name()`;
        - a RETURN of a handle class wraps in it - the C++ hands back
          what it holds, and Python must get the handle;
        - a PARAMETER of a handle class unwraps out of it, because the
          C++ takes what the handle points at.

        ...and one a plain STRUCT forces: `@reads` names a data member,
        so there is nothing to call - `self.narSize`, not `self.narSize()`.
        A member read cannot be bound by pointer the way a method can,
        because `.def` takes a function and `&T::narSize` is not one.

        None when this method needs none of the four. `_method` then
        binds it by pointer, which is the shorter and better line."""
        # A guarded accessor reaches through the union pair rather than
        # through `via`, so the two are read separately and `call` below
        # is rebuilt after the guard picks its reach.
        # A `list[T]` return needs a body too: the conversion below is
        # what makes a C++ set answer the list the declaration promised,
        # and a pointer binding has nowhere to put it.
        wants_list = (m.returns is not None
                      and m.returns.origin in ("list", "dict"))
        if not (cls.decl.via or m.returns_handle or m.reads or m.guard
                or m.names or m.produces or wants_list
                or any(pr.via for pr in m.params)):
            return None
        obj = _self(cls)
        reach = f"{obj}.{cls.decl.via}->" if cls.decl.via else f"{obj}."
        # The GUARD, for an accessor on a tagged union.
        #
        # Written here, once, from two declared facts: the class says how
        # to ask which arm is held, and the accessor says which one it
        # needs. Twelve accessors used to spell this by hand in
        # `cpp/eval.hpp`, and a thirteenth could have forgotten it -
        # which for a `noexcept` reader on the wrong tag is not an error
        # but a reinterpretation of the payload.
        # A PRODUCER: allocate on this state, call one initialiser with
        # the declared arguments, wrap. The declaration names only the
        # initialiser; the three lines around it are the same for every
        # producer, which is why they are here and not in twelve bodies.
        if m.produces:
            given = ", ".join(pr.name for pr in m.params)
            return [
                f"{INDENT * 4}auto * made = {obj}.alloc();",
                f"{INDENT * 4}made->{m.produces}({given});",
                f"{INDENT * 4}return {obj}.wrap(made);",
            ]

        head = self._guard_head(cls, m)
        if m.guard is not None:
            reach = f"{obj}.{m.guard.hold}->"
        if m.names is not None:
            # The arm table, read the other way. One switch, so the names
            # a caller sees and the names @guard checks cannot drift.
            lines = [f"{INDENT * 4}switch ({obj}.{m.names.hold}->"
                     f"{m.names.ask}) {{"]
            for name, enum in m.names.arms:
                lines.append(f'{INDENT * 4}case {enum}: return "{name}";')
            lines.append(f"{INDENT * 4}}}")
            # Every enumerator is named above, and a compiler still wants
            # a return past the switch.
            lines.append(f'{INDENT * 4}return "unknown";')
            return lines
        # A declared `list[T]` PARAMETER over a C++ set. libstore takes
        # StorePathSet in a dozen places and the wire carries a list, so
        # the conversion is a fact about the two type systems rather than
        # a decision - and `as_set` is emitted beside this, not written by
        # hand.
        # A declared `list[T]` PARAMETER, where T's class says libstore
        # holds a collection of them some other way. The wire carries a
        # list either way; this is the two type systems disagreeing, not a
        # decision, so `as_set` is written here rather than at each site.
        passed = ", ".join(
            f"as_set<{pr.collection}>({pr.name})" if pr.collection else
            (f"{pr.name}.{pr.via}" if pr.via else pr.name)
            for pr in m.params)
        # A member is reached, not called. The declaration says which by
        # writing @reads, and an accessor that reads one takes no
        # parameters - so there is no argument list to spell either.
        call = (f"{reach}{m.reads}" if m.reads
                else f"{reach}{m.cxx_name or m.name}({passed})")
        if m.returns is None:
            return [*head, f"{INDENT * 4}{call};"]
        if m.returns_handle:
            return [*head, f"{INDENT * 4}return {m.returns_handle}({call});"]
        # A declared `Bytes` over a string, and the list of each. A string
        # is not one implicitly - `nb::bytes` takes only explicit
        # constructors - so the conversion is written here, once.
        if m.returns.cxx in BYTES_SPELLINGS:
            return [*head, f"{INDENT * 4}return to_bytes({call});"]
        # A declared `list[T]` RETURN over a C++ collection that is not
        # a vector. libstore answers with a set almost everywhere, and the
        # declaration already said `list` - so nothing needs to say it
        # twice. `as_list` is a template over any range, so wrapping is
        # right whether the call answered a set or a vector.
        if m.returns.origin == "list":
            return [*head, f"{INDENT * 4}return as_list({call});"]
        # The same for a `dict[str, V]`: libstore keys many maps with a
        # transparent `std::less<>`, and nanobind casts only the plain one.
        if m.returns.origin == "dict":
            return [*head, f"{INDENT * 4}return as_map({call});"]
        # A declared VOCABULARY return. The enumerator libstore answers
        # with is not the word Python has, and `as_word` is the switch
        # that says which - emitted beside this, not written by hand.
        # Before this, `Hash.algorithm` carried the conversion as a `Cxx`
        # body, which is a MAPPING written into a declaration.
        if m.returns_word:
            return [*head, f"{INDENT * 4}return {NAMESPACE}::as_word({call});"]
        # A width the DECLARATION spells. `size()` answers a size_t and
        # the declaration says I64, so the cast is what makes the emitted
        # C++ say what the declaration says rather than what this
        # library's version of the call happens to return.
        spelled = m.returns.cxx
        if spelled in ("std::int64_t", "std::uint64_t"):
            return [*head, f"{INDENT * 4}return static_cast<{spelled}>({call});"]
        return [*head, f"{INDENT * 4}return {call};"]

    def _guard_head(self, cls: ir.ClassModel,
                    m: ir.MethodModel) -> list[str]:
        """The tag check an accessor on a tagged union owes its caller.

        Separate from HOW the rest of the body reads, so a method with a
        declared `Cxx` body gets one too. A body is a decision about what
        to DO once the arm is known; the check that the arm IS known is
        the same either way, and writing it inside twelve bodies is what
        this exists to stop."""
        if m.fills is not None:
            # The FIRST parameter is the value being filled. A method that
            # fills takes its target first, which is what makes this
            # derivable rather than another thing to name.
            target, arm = m.fills.target, m.fills.arm
            return [
                # The arm FIRST. A builder of the wrong kind passes the
                # builder test and then reads the wrong union member,
                # which is undefined rather than an error.
                f"{INDENT * 4}if ({target}.{arm.hold}->{arm.ask} != {arm.tag})",
                f"{INDENT * 5}{_wrong_arm(target, arm.hold, arm.tag)}",
                f"{INDENT * 4}if (!{target}.is_builder())",
                f"{INDENT * 5}throw std::invalid_argument(",
                f'{INDENT * 6}"this value did not come from {m.fills.maker}, and a "',
                f'{INDENT * 6}"Nix value is immutable: filling it would "',
                f'{INDENT * 6}"rewrite memory the evaluator produced");',
            ]
        if m.guard is None:
            return []
        obj, arm = _self(cls), m.guard
        return [
            f"{INDENT * 4}if ({obj}.{arm.hold}->{arm.ask} != {arm.tag})",
            f"{INDENT * 5}{_wrong_arm(obj, arm.hold, arm.tag)}",
        ]

    def _returns(self, m: ir.MethodModel) -> str:
        """A lambda's return type, SPELLED, for every body that has one.

        It started as an optional-only rule - a lambda with two return
        paths, the value and std::nullopt, cannot deduce one - and the
        same argument covers more than optionals. A body ending
        `return {};` for an empty container cannot deduce either, and
        neither can two returns whose types merely convert. The
        declaration already said which type it is, so saying it in the
        lambda costs nothing and removes the whole class of "cannot
        deduce".

        Both branches of `_method`, because the argument never was about
        who WROTE the body. A declared body had it and a derived body did
        not, so `@reads` over a `list[T]` deduced `as_list`'s return
        where the same accessor with a `Cxx` line spelled it - one fact,
        stated in one branch of two.

        Empty for a method that returns nothing. `force` and the
        builders' setters do, and a lambda with no return statement is
        void.
        """
        if m.returns is None:
            return ""
        return f" -> {m.returns.cxx}"

    def _method(self, cls: ir.ClassModel, m: ir.MethodModel) -> list[str]:
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
        doc = _doc(m.doc)
        tail = f', "{doc}"' if doc else ""
        if m.cxx_body:
            # A method the declaration could not derive, carried verbatim.
            obj = _self(cls)
            args, opening = self._signature(m.params)
            head = (f'{INDENT * 2}.def("{m.name}", '
                    f"[]({_held(cls)} &{obj}{args}){self._returns(m)} {{")
            body = [f"{INDENT * 4}{ln}".rstrip()
                    for ln in m.cxx_body.strip().splitlines()]
            # The tag check goes in FRONT of a declared body. A body says
            # what to do once the arm is known; @guard says the arm is
            # known, and the two are separate decisions.
            extras = self._extras(waits(cls, m), m.params)
            return [head, *opening, *self._guard_head(cls, m), *body,
                    f"{INDENT * 2}}}{extras}{tail})"]
        extras = self._extras(waits(cls, m), m.params)
        derived = self._derived(cls, m)
        if derived is not None:
            obj = _self(cls)
            args, opening = self._signature(m.params)
            return [f'{INDENT * 2}.def("{m.name}", '
                    f"[]({_held(cls)} &{obj}{args}){self._returns(m)} {{",
                    *opening, *derived,
                    f"{INDENT * 2}}}{extras}{tail})"]
        spelled = m.cxx_name or m.name
        return [f'{INDENT * 2}.def("{m.name}", &{_held(cls)}::{spelled}'
                f"{extras}{tail})"]

    def _signature(self, params: Sequence[ir.ParamModel],
                   ) -> tuple[str, list[str]]:
        """A lambda's parameter list, and the lines that open its body.

        Almost always the first alone. The lines exist for one case: a
        container that reads None. It arrives as a std::optional, and the
        body wants the plain container - so the parameter is renamed and
        one derived line puts the declared name back, holding an empty
        container where the caller passed nothing.

        The body then reads exactly what the declaration wrote."""
        args, opening = "", []
        for pr in params:
            spelled = pr.cxx
            parser = pr.parsed_by
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
            if not pr.absent:
                args += f", {spelled} {pr.name}"
                continue
            held = pr.type.required.cxx
            args += f", const std::optional<{held}> & {pr.name}_"
            opening.append(f"{INDENT * 4}const {held} {pr.name} = "
                           f"{pr.name}_.value_or({held}{{}});")
        return args, opening

    def _identity_semantics(self, cls: ir.ClassModel,
                            equality: bool = True) -> list[str]:
        """The repr and the hash every wire value owes a reader.

        Both from the declared PARTS, and both through the Python object.
        `nb::repr(h.attr("path")())` asks StorePath for its own repr,
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
        # A UNIT value has no parts and still compares, hashes and prints:
        # every one is equal, `Deferred()` names it, and the empty tuple
        # hashes.
        if not fields and not cls.decl.unit:
            return []
        spec = ", ".join(f"{name}={{!r}}" for name, _, _ in fields)
        reads = ", ".join(read for _, _, read in fields)
        hashed = ", ".join(
            f"{NAMESPACE}::as_tuple({read})" if t.required.origin == "list"
            else read
            for _, t, read in fields)
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

    def _ctor(self, cls: ir.ClassModel) -> list[str]:
        """`nb::init<...>`, with the declared parameter named for Python.

        `"name"_a` is what makes the parameter usable as a keyword, so the
        declaration's parameter NAME reaches callers rather than being
        decoration."""
        init = cls.init
        if init is None:
            return []
        names = "".join(
            f', "{pr.name}"_a'
            + (f" = {pr.cxx_default}" if pr.cxx_default else "")
            for pr in init.params)
        if init.cxx_body:
            # Placement new, because `__init__` is handed storage rather
            # than asked for an object. One Python signature over several
            # C++ constructors needs this: nb::init picks by C++ type at
            # compile time, and which constructor to call is a decision
            # about a VALUE - an OutputsSpec means all outputs when it says
            # so and a named set when it carries names.
            obj = _self(cls)
            args, opening = self._signature(init.params)
            body = [f"{INDENT * 4}{ln}".rstrip()
                    for ln in init.cxx_body.strip().splitlines()]
            head = (f'{INDENT * 2}.def("__init__", '
                    f"[]({_held(cls)} *{obj}{args}) {{")
            tail = f"{INDENT * 2}}}{names}"
            if not init.doc:
                return [head, *opening, *body, tail + ")"]
            doc = _doc(init.doc)
            return [head, *opening, *body, tail + ",",
                    f'{INDENT * 3}     "{doc}")']
        types = ", ".join(pr.cxx for pr in init.params)
        line = f"{INDENT * 2}.def(nb::init<{types}>(){names}"
        if not init.doc:
            return [line + ")"]
        # One line, however the declaration wrapped it: a C++ string
        # literal has no continuation and gluing two is noise.
        doc = _doc(init.doc)
        return [line + ",", f'{INDENT * 3}     "{doc}")']

    def record_fields(self, cls: ir.ClassModel) -> list[tuple[str, str]]:
        """Every member of a produced value's struct, in declared order.

        Nothing is listed. A produced value's ACCESSORS are its fields -
        the store flattened one of its own objects and handed over the
        parts - so the name is the accessor's name and the type is what
        it returns. Order is the declaration's, which is the order a
        reader of the declaration sees and the order the constructor
        takes."""
        return [(m.name, m.returns.cxx)
                for m in cls.bound if m.returns is not None and not m.local]

    def record(self, cls: ir.ClassModel) -> list[str]:
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
                for name, spelling in self.record_fields(cls)]
        # A value compares as its parts, and `= default` is the whole of
        # that sentence. Written out, it would be one line per field with
        # nothing to gate it against the field list.
        out += [f"{INDENT}bool operator==(const {cls.name} &) const = default;",
                "};"]
        return out

    def records(self, classes: Sequence[ir.ClassModel]) -> list[str]:
        """Every produced value in one unit, inside one namespace."""
        values = [c for c in classes if c.is_value]
        if not values:
            return []
        out = [f"namespace {NAMESPACE} {{", ""]
        for cls in values:
            out += [*self.record(cls), ""]
        return [*out, f"}}  // namespace {NAMESPACE}", ""]

    def records_named(self, classes: Sequence[ir.ClassModel],
                      functions: Sequence[ir.FunctionModel]) -> list[str]:
        """Every module whose records this unit names, its own included.

        A record is a struct the emitter declares, so a unit that names one
        from another module needs that module's struct, not only its
        Python class. Sorted, for `includes`' reason."""
        out = {c.module for c in classes if c.is_value}
        for _, t in _sites(classes, functions):
            for node in _nodes(t):
                held = self.model.declared(node)
                if (isinstance(held, ir.ClassModel) and held.decl.produced
                        and not held.decl.cxx):
                    out.add(held.module)
        return sorted(out)

    def _unions_used(self, classes: Sequence[ir.ClassModel],
                     functions: Sequence[ir.FunctionModel],
                     ) -> list[ir.UnionModel]:
        """Every union with a declared C++ variant this unit names.

        By name, so a union named twice is converted once. Sorted,
        because the order two methods happen to be declared in is not an
        order for a translation unit."""
        out: dict[str, ir.UnionModel] = {}
        for _, t in _sites(classes, functions):
            for node in _nodes(t):
                held = self.model.declared(node)
                if (isinstance(held, ir.UnionModel)
                        and held.decl.variant is not None):
                    out[node.name] = held
        return [out[name] for name in sorted(out)]

    def _vocabularies_used(self, classes: Sequence[ir.ClassModel],
                           functions: Sequence[ir.FunctionModel],
                           ) -> list[ir.EnumModel]:
        """Every enum-backed vocabulary this unit NAMES, either way round.

        Every site, not only the returns. A unit that only TAKES a word
        needs no read-back conversion to work, and gets one anyway,
        because the read-back conversion is the switch and the switch is
        the gate. `Store.build_paths` takes a BuildMode and returns none,
        so without this the day upstream adds a fourth mode would pass
        silently. The emitted function is `inline` and unused, which
        costs a compiler nothing.

        By name and sorted, for the reason `_unions_used` is: a
        vocabulary named twice is converted once, and the order two
        methods happen to be declared in is not an order for a
        translation unit."""
        out = {node.name: held for _, t in _sites(classes, functions)
               for node in _nodes(t)
               if isinstance(held := self.model.declared(node), ir.EnumModel)
               and held.decl.enumerated}
        # ...and what a BODY spells, from `@spells`. A signature does not
        # reach everything: `KeyedBuildResult.error` builds an exception
        # carrying a failure word, and `-> BuildError | None` says
        # nothing about it. The model checked each is an enumerated
        # vocabulary.
        spelled = [n for cls in classes for m in cls.bound for n in m.spells]
        spelled += [n for cls in classes if cls.from_parts is not None
                    for n in cls.from_parts.spells]
        spelled += [n for fn in functions for n in fn.spells]
        out.update((name, self.model.enums[name]) for name in spelled)
        return [out[name] for name in sorted(out)]

    def _alternative(self, cls: ir.UnionModel, arm: str) -> tuple[str, str]:
        """One arm as the C++ variant holds it: the type, and the member.

        The member is empty when the variant holds the arm as itself,
        which is every arm no `wraps` names. `SingleDerivedPathBuilt` is
        an alternative of `nix::SingleDerivedPath` outright, so nothing
        has to be said about it - and saying it for every arm would make
        the one arm that IS wrapped read like the others."""
        variant = cls.decl.variant
        assert variant is not None
        wrap = variant.wraps.get(arm)
        if wrap is not None:
            return wrap.cxx, wrap.holds
        return _arm(cls, arm), ""

    def conversions(self, cls: ir.UnionModel) -> list[str]:
        """One union, in both directions, from what its alias declares.

        THE BODIES DO NOT SAY THIS. They call `as_arms` and `from_arms`
        by name, and every line below comes from the `Variant(...)` the
        declaration carries - the union's C++ type, how to reach the
        std::variant inside it, and which arm the variant wraps.

        It was a hand-written header (huggorm#63). Two unions wrote the
        same visit four times, and the two `drv_path` bodies that called
        it were identical text in two classes that differ in nothing the
        line touches. One declared fact answers all of it.

        The last arm is `std::get` rather than another `std::get_if`.
        A variant holds exactly one alternative, so once every other has
        been ruled out the last one is what is there - and `std::get`
        says that, where a fourth `if` would leave a fall-through with
        nothing to return.
        """
        variant = cls.decl.variant
        assert variant is not None
        arms = cls.decl.arms
        held = _arms_type(cls)
        reach = f"p.{variant.raw}" if variant.raw else "p"
        out = [f"/** The arms of a {cls.name}, as Python has them. */",
               f"inline {held} as_arms(const {variant.cxx} & p)",
               "{"]
        for arm in arms[:-1]:
            alt, member = self._alternative(cls, arm)
            out += [f"{INDENT}if (auto * arm = std::get_if<{alt}>(&{reach}))",
                    f"{INDENT * 2}return {'arm->' + member if member else '*arm'};"]
        alt, member = self._alternative(cls, arms[-1])
        got = f"std::get<{alt}>({reach})"
        out += [f"{INDENT}return {got + '.' + member if member else got};",
                "}", ""]

        out += [f"/** A {cls.name}'s arms, as the C++ union holds them. */",
                f"inline {variant.cxx} from_arms(const {held} & a)",
                "{"]
        for arm in arms[:-1]:
            alt, member = self._alternative(cls, arm)
            out += [f"{INDENT}if (auto * arm = "
                    f"std::get_if<{_arm(cls, arm)}>(&a))",
                    f"{INDENT * 2}return {alt + '{*arm}' if member else '*arm'};"]
        last = arms[-1]
        alt, member = self._alternative(cls, last)
        got = f"std::get<{_arm(cls, last)}>(a)"
        out += [f"{INDENT}return {alt + '{' + got + '}' if member else got};",
                "}", ""]

        return out

    def bare_check(self, cls: ir.UnionModel) -> list[str]:
        """What a `bare` union gets instead of a conversion and a caster.

        The claim that the C++ type IS the arms' `std::variant`, checked by
        the compiler. A declaration that lists the arms out of order, or
        names a union that only derives from a variant, fails here rather
        than casting through the wrong alternative."""
        variant = cls.decl.variant
        assert variant is not None
        return [f"static_assert(std::is_same_v<{variant.cxx}, "
                f"{_arms_type(cls)}>,",
                f'{INDENT}"{cls.name} is declared bare, so its C++ type must '
                f'be the std::variant of its arms, in order");', ""]

    def caster(self, cls: ir.UnionModel) -> list[str]:
        """One union as a nanobind type_caster, so no body converts.

        `as_arms` and `from_arms` still do the work; this is where they
        are CALLED, once, instead of at every site that names the type.
        The signature then says `nix::DerivedPath` - what libstore says -
        and `store.parse_derived_path` is one call with nothing round it.

        Composed with the caster nanobind ships for std::variant rather
        than written out. The arms already cast; the only thing missing
        was that `nix::DerivedPath` IS a variant and nanobind's caster is
        specialised on `std::variant` exactly, not on something deriving
        from one.

        NB_TYPE_CASTER is not used, for the reason nanobind's own variant
        caster does not use it: the macro declares `Value value;` and
        neither union is default-constructible - the opaque arm holds a
        `nix::StorePath`, which has no default constructor. The storage is
        an optional instead, and the three cast operators reach through
        it.
        """
        variant = cls.decl.variant
        assert variant is not None
        arms = _arms_type(cls)
        return [
            f"/** {variant.cxx}, cast as the arms Python has. */",
            f"template <> struct type_caster<{variant.cxx}> {{",
            f"{INDENT}using Value = {variant.cxx};",
            f"{INDENT}using Arms = {arms};",
            f"{INDENT}using Caster = make_caster<Arms>;",
            f"{INDENT}static constexpr auto Name = Caster::Name;",
            f"{INDENT}template <typename T_> using Cast = movable_cast_t<T_>;",
            f"{INDENT}template <typename T_> static constexpr bool can_cast()"
            f" {{ return true; }}",
            "",
            f"{INDENT}std::optional<Value> held;",
            f"{INDENT}explicit operator Value *() {{ return &*held; }}",
            f"{INDENT}explicit operator Value &() {{ return *held; }}",
            f"{INDENT}explicit operator Value &&() {{ return (Value &&) *held; }}",
            "",
            f"{INDENT}bool from_python(handle src, uint8_t flags,",
            f"{INDENT * 4}     cleanup_list *cleanup) noexcept",
            f"{INDENT}{{",
            f"{INDENT * 2}Caster caster;",
            f"{INDENT * 2}if (!caster.from_python(src, flags, cleanup))",
            f"{INDENT * 3}return false;",
            f"{INDENT * 2}held.emplace({NAMESPACE}::from_arms("
            f"caster.operator cast_t<Arms>()));",
            f"{INDENT * 2}return true;",
            f"{INDENT}}}",
            "",
            f"{INDENT}static handle from_cpp(const Value &value, rv_policy policy,",
            f"{INDENT * 4}               cleanup_list *cleanup) noexcept",
            f"{INDENT}{{",
            f"{INDENT * 2}return Caster::from_cpp({NAMESPACE}::as_arms(value), "
            f"policy, cleanup);",
            f"{INDENT}}}",
            "",
            f"{INDENT}static handle from_cpp(const Value *value, rv_policy policy,",
            f"{INDENT * 4}               cleanup_list *cleanup) noexcept",
            f"{INDENT}{{",
            f"{INDENT * 2}if (value == nullptr)",
            f"{INDENT * 3}return none().release();",
            f"{INDENT * 2}return from_cpp(*value, policy, cleanup);",
            f"{INDENT}}}",
            "};",
            "",
        ]

    def _record_semantics(self, cls: ir.ClassModel) -> list[str]:
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

    def _record_ctor(self, cls: ir.ClassModel) -> list[str]:
        """The two ways a record is and is not built.

        A produced value is PRODUCED. Nothing a caller does should build
        one from nothing, and `__init__` says so, naming the calls that
        make one - so a caller who guesses wrong is told where to look
        instead of getting an argument-count error.

        It still has to be RECONSTRUCTIBLE, because it crosses the wire
        and the far side has only the parts. So the constructor is bound
        privately, as `_from_parts`, which is exactly the name the wire
        layer asks for."""
        if cls.from_parts is not None:
            # A declared body: the signature still comes from the field
            # list, so the body can only consume what `_parts` sent.
            return [*self._produced_ctor(cls), *self._from_parts(cls)]
        fields = self.record_fields(cls)
        if any(f.read != f.name for f in cls.wire_fields):
            # A part read through another accessor - `@wire_read` - arrives
            # as that accessor's type, so the aggregate needs the parts
            # converted back. `_from_parts` initialises POSITIONALLY, so
            # the model checks the parts are the members, in member order.
            return [*self._produced_ctor(cls), *self._from_parts(cls)]
        held = _held(cls)
        # `.none()` on an `nb::object` part, and nothing else needs it.
        # nanobind refuses None for a parameter unless the argument says
        # it takes one, and an `nb::object` caster accepts anything - so
        # the refusal is the ARGUMENT's, not the caster's. Measured: a
        # KeyedBuildResult with no failure arm could not be rebuilt at
        # all, and the message named every parameter as compatible
        # (huggorm#71).
        args = "".join(f', "{name}"_a' + (".none()" if spelling == "nb::object"
                                          else "")
                       for name, spelling in fields)
        made = ", ".join(f"{spelling} {name}" for name, spelling in fields)
        values = ", ".join(name for name, _ in fields)
        return [
            *self._produced_ctor(cls),
            f'{INDENT * 2}.def_static("_from_parts", []({made}) {{',
            f"{INDENT * 3}return {held}{{{values}}};",
            f'{INDENT * 2}}}{args}, "{FROM_PARTS_DOC}")',
        ]

    def _factory(self, cls: ir.ClassModel) -> list[str]:
        """`nb::new_`, for a class something else makes.

        nix::Store is abstract and its implementation is chosen by a URI,
        so there is no constructor to bind - and `Store(uri)` is still the
        Python surface, because that is what the declaration's `__init__`
        says. `nb::new_` is exactly that shape: a factory returning a
        handle, bound as `__new__`.

        Both halves are declared, in two places that already had to
        agree. `@constructs(Store)` marks the factory; the free function
        called `open_store` names the C++ it binds. So this resolves one
        through the other and neither declaration repeats the other's
        spelling.

        The extras come from the FACTORY, not from the `__init__` beside
        it, because the factory is what runs. `open_store` carries
        `@blocks` - opening a daemon store connects, and a local one may
        create its database - and it carries the default `uri="auto"`.
        Reading them off the constructor instead dropped both: the call
        held the GIL for the length of an open, and `Store()` raised
        where the declaration said it should work."""
        made = cls.factory
        if cls.init is None or made is None:
            # A factory this declaration does not carry. The class is
            # still bound; it just offers no way in, which is the honest
            # answer until the factory is declared too.
            return []
        # Two shapes, and the declaration already says which. A factory
        # that NAMES its C++ is an address; one that CARRIES it is a
        # lambda, and the body goes inside.
        #
        # Before this, a factory had to be a named symbol, so a class
        # whose construction needed one line of adaptation had to put a
        # helper in `cpp/`. That is how `huggorm::open_store` came to
        # exist: three lines wrapping one call, because the emitter could
        # not write it (huggorm#63).
        extras = self._extras(waits(cls, made), made.params)
        doc = _doc(cls.init.doc) if cls.init.doc else ""
        if not made.cxx_body:
            line = f"{INDENT * 2}.def(nb::new_(&{made.cxx_name}){extras}"
            if not doc:
                return [line + ")"]
            # One line, however the declaration wrapped it: a C++ string
            # literal has no continuation and gluing two is noise.
            return [line + ",", f'{INDENT * 3}     "{doc}")']
        lines = [f"{INDENT * 2}.def(nb::new_({self._lambda_head(made)}",
                 *(f"{INDENT * 3}{ln}".rstrip()
                   for ln in made.cxx_body.strip().splitlines())]
        close = f"{INDENT * 2}}}){extras}"
        if not doc:
            return [*lines, close + ")"]
        return [*lines, close + ",", f'{INDENT * 3}     "{doc}")']

    def _lambda_head(self, fn: ir.FunctionModel) -> str:
        """The opening of a lambda for a function that CARRIES its C++.

        The return type is SPELLED, for the reason `_method` spells one:
        a body whose returns merely CONVERT to the declared type, or that
        ends `return {}`, cannot deduce it. A free body and a factory
        body are the same kind of body, so they take the same rule - it
        was applied to a method and to neither of these, which is one
        rule in one place out of three.

        NO CURRENT BODY NEEDS IT, and that is worth writing down because
        the first version of this comment claimed otherwise. It said
        `nix::openStore` returning a `nix::ref<Store>` would not deduce.
        Dropping the spelling and rebuilding refuted that: nanobind takes
        the `ref` and reaches the holder through its implicit conversion
        to `shared_ptr`, and the module compiles. Two emitted lines
        change - this one and `gc_stats` - and both compile either way.

        So this is consistency, not a fix. It is kept because the rule is
        real for bodies a person may write next, and because one rule
        spelled three ways is what this repo exists to avoid."""
        args = ", ".join(f"{pr.cxx} {pr.name}" for pr in fn.params)
        ret = f" -> {fn.returns.cxx}" if fn.returns is not None else ""
        return f"[]({args}){ret} {{"

    def part_types(self, cls: ir.ClassModel) -> list[str]:
        """The C++ each part ARRIVES as, one per wire field.

        From the accessor's own annotation wherever there is one, because
        that is the only place the width lives: `int` is the wire spelling
        of both `nar_size` and `registration_time`, and they are a
        uint64_t and an optional int64_t. `list[StorePath]` has no wire
        spelling at all.

        Every part has an accessor: `Class.parts` refuses one that does
        not."""
        return [f.type.cxx for f in cls.wire_fields]

    def _rebuilt(self, f: ir.FieldModel) -> str:
        """One part, converted back to what the C++ member IS, or "".

        A part arrives as the wire carries it, and a `list[T]` is a
        vector. Where the member is a SET, aggregate initialisation does
        not convert - `could not convert 'paths' from
        'std::vector<std::string>' to 'nix::StringSet'` is what the
        compiler says, and it says it about a line this emitter wrote.

        Two declarations answer, and they are one fact stated at two
        scopes. `@binding(collection=...)` on the ELEMENT class says every
        `list[StorePath]` is a `nix::StorePathSet`. `@reads(member,
        collection=...)` says THIS member is, and it exists for the case
        with no element class to ask: `str` is a builtin, and
        `nix::GCResults::paths` is a `StringSet`.

        The field's own answer wins. It is the more specific of the two,
        and a class-wide rule that could not be overridden would make the
        exception unsayable.

    Both branches have a user. `GCResults.paths` needs the field's,
        and `MissingPaths` needs the class's - its three path lists are
        `StorePathSet`s, and it used to carry a declared `_from_parts`
        whose whole body was the three `as_set` calls this now writes.
        That body is gone, which is the point: it was a mapping, and a
        mapping is derived.

        `PathInfo` still writes its own, for reasons that are not this
        one - a virtual base, so it is not an aggregate at all."""
        if not f.collection:
            return ""
        # The lambda's parameter is the part's NAME. `f.read` names an
        # accessor, which is nothing inside `_from_parts`.
        return f"as_set<{f.collection}>({f.name})"

    def _from_parts(self, cls: ir.ClassModel) -> list[str]:
        """`_from_parts`, for a value nothing constructs.

        A wire value has to be rebuildable from its parts: it crosses as a
        message and the far side has only those. Where a public
        constructor takes exactly the parts, `markers` names the class and
        there is nothing to write. Where there is no public constructor,
        the C++ one still takes them - PathInfo refuses
        `PathInfo(...)` in Python and nix::ValidPathInfo takes its fields
        happily - so this calls it directly, under the private
        name the wire layer asks for.

        And where neither is true, the DECLARATION carries the body.
        `nix::ValidPathInfo` has a virtual base, so it is not an aggregate
        and cannot be brace-initialised; its only constructor takes an
        `UnkeyedValidPathInfo`; and two of its parts cross rendered and
        are parsed back. That is a decision rather than a binding, and it
        goes where a person writes code.

        The SIGNATURE stays here either way. One typed parameter per wire
        field, in the field list's order, so a declared body cannot
        disagree with `_parts` about what crosses or in which order - it
        can only consume what it is handed."""
        fields = wire_fields(cls)
        if not fields and not cls.decl.unit:
            return []
        types = self.part_types(cls)
        args = ", ".join(f"{t} {n}" for (n, _, _), t in zip(fields, types,
                                                            strict=True))
        # `.none()` on an `nb::object` part, and nothing else needs it.
        # nanobind refuses None for a parameter unless the ARGUMENT says
        # it takes one, and an nb::object caster accepts anything - so the
        # refusal is the argument's rather than the caster's. Measured: a
        # KeyedBuildResult with no failure arm could not be rebuilt at
        # all, and the message listed every parameter as compatible
        # (huggorm#71).
        keywords = "".join(f', "{n}"_a' + (".none()" if t == "nb::object" else "")
                           for (n, _, _), t in zip(fields, types, strict=True))
        written = cls.from_parts
        if written is not None and written.cxx_body:
            body = [f"{INDENT * 3}{ln}".rstrip()
                    for ln in written.cxx_body.strip().splitlines()]
        else:
            names = ", ".join(
                f"from_bytes({n})" if t in BYTES_SPELLINGS
                else self._rebuilt(f) or n
                for (n, _, _), f, t in zip(fields, cls.wire_fields, types,
                                           strict=True))
            body = [f"{INDENT * 3}return {_held(cls)}({names});"]
        doc = _doc(written.doc) if written is not None and written.doc \
            else FROM_PARTS_DOC
        return [f'{INDENT * 2}.def_static("_from_parts", []({args}) {{',
                *body,
                f'{INDENT * 2}}}{keywords}, "{doc}")']

    def bind_function(self, cls: ir.ClassModel) -> str:
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
        holds = [held]
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
        # A PROXY is not final: a caller may subclass one to add
        # behaviour, and nothing about a handle breaks when they do.
        final = ", nb::is_final()" if decl.wire == "value" else ""
        # The class's own prose, which a declaration always writes and a
        # caller could not read: `help(StorePath)` answered with nothing
        # but the signature until this line existed.
        doc = _doc(cls.doc)
        shown = f', "{doc}"' if doc else ""
        # GC slots, when the class holds Python objects. The declaration
        # names a `PyType_Slot[]` and the helper supplies it; there is
        # nothing here to derive, because a traversal is a function over a
        # member no declaration describes. Without it a class that stores
        # a callable leaks itself as soon as that callable closes over it
        # (huggorm#93).
        slots = (f", nb::type_slots({decl.gc_slots})"
                 if decl.gc_slots else "")
        lines = [f"static void bind_{cls.name.lower()}(nb::module_ &m) {{",
                 f'{INDENT}auto cls = nb::class_<{", ".join(holds)}>'
                 f'(m, "{cls.name}"{shown}{final}{slots})']
        if cls.is_value:
            # A RECORD: the emitter declared the struct, so every accessor
            # is a member and the whole binding is derived from the field
            # list.
            body = self._record_ctor(cls)
            obj = _self(cls)
            # METHODS, not `def_ro` properties. The declaration writes
            # `def path(self) -> StorePath`, so a caller writes
            # `info.path()`. A property would read better and would be a
            # DIFFERENT surface, which is not a choice an emitter makes
            # on its own.
            # ...each carrying the prose the declaration wrote for it.
            # The accessor is derived from the field list, but what the
            # field MEANS is a sentence only a person can write, and the
            # declaration already has one on every method.
            described = {m.name: _doc(m.doc) for m in cls.bound}
            body += [f'{INDENT * 2}.def("{name}", [](const {held} &{obj}) '
                     f"{{ return {obj}.{name}; }}"
                     + (f', "{described[name]}"' if described.get(name) else "")
                     + ")"
                     for name, _ in self.record_fields(cls)]
            body += self._record_semantics(cls)
            # ...which owns `__eq__` for a record, so this contributes
            # the repr and the hash alone.
            body += self._identity_semantics(cls, equality=False)
            body += _round_trip(cls)
            body += _value_semantics(cls)
            # A `@local` method still binds: local keeps it off the wire,
            # not off the object. The struct has no member for it, so it
            # needs a body or a `@reads`; with neither, `_method` names a
            # member that does not exist and the unit fails to compile,
            # which is the loud answer rather than a quiet absence.
            for m in cls.bound:
                if m.local:
                    body += self._method(cls, m)
            if body:
                body[-1] += ";"
            return "\n".join([*lines, *body, *markers(cls), "}"]) + "\n"
        # No nb::init when something else builds one: there is no
        # constructor to call. A FACTORY takes its place where the
        # declaration names one. An ABSTRACT class offers neither: a
        # caller holds one all the time and constructs one never.
        if not cls.constructs:
            # No constructor, and a refusal that says so.
            #
            # The DOOR, not the C++ fact. `nix::Store` is abstract AND
            # opened by a factory, so it takes the branch below - reading
            # `decl.abstract` here sent it to this one instead and
            # `Store("dummy://")` stopped existing (huggorm#61).
            #
            # It used to be a `pointer_and_handle` guard, which refused a
            # direct call and let a SUBCLASS through - because a Python
            # class deriving from this one was instantiated as the
            # trampoline. There are no trampolines any more (huggorm#60),
            # so there is no door to hold open and no reason for the
            # binding to know what `nb_inst_python_derived` is.
            # The sentence says WHICH way the door is shut, because the
            # two are different mistakes: a declaration that forgot an
            # `__init__`, and one that is honestly abstract with nothing
            # to open it. `_produced_ctor` supplies the third wording
            # itself, from the calls that return the class.
            body = self._produced_ctor(cls, "" if cls.decl.produced else (
                "declares no constructor" if cls.init is None
                else "is abstract, and no factory opens one"))
        elif decl.factory:
            # A factory this module BINDS - `open_store` becomes
            # `Store.__new__`.
            body = self._factory(cls)
        else:
            body = self._ctor(cls)
        for m in cls.bound:
            body += self._method(cls, m)
        if decl.wire == "value":
            body += self._identity_semantics(cls)
            body += _round_trip(cls)
            if cls.init is None:
                body += self._from_parts(cls)
        body += _value_semantics(cls)
        for source in decl.custom.values():
            body += [f"{INDENT * 2}{line}".rstrip()
                     for line in source.splitlines()]
        if body:
            body[-1] += ";"
        return "\n".join([*lines, *body, *markers(cls), "}"]) + "\n"

    def free_function(self, fn: ir.FunctionModel) -> list[str]:
        """One `m.def`, for a function that belongs to no class.

        nanopynix has 72 of these and they are one shape:
        `m.def("open_store", &open_store_uri, "uri"_a)`. The C++ helper is
        hand-written - `open_store_uri` keeps a per-state-directory cache,
        because two LocalStores in one process deadlock on a temp-roots
        flock - and the declaration names it rather than pretending to
        have written it.

        `blocking` has no class to come from here, so a free function says
        `@blocks` for itself."""
        if not (fn.cxx_name or fn.cxx_body):
            raise TypeError(
                f"{fn.name}: a free function names the C++ it binds, or "
                f'carries it. Use @binds("cxx_name") or @cxx_body(...).')
        extras = []
        if fn.blocks and not fn.instant:
            extras.append("nb::call_guard<nb::gil_scoped_release>()")
        for pr in fn.params:
            arg = f'"{pr.name}"_a'
            if pr.cxx_default:
                arg += f" = {pr.cxx_default}"
            extras.append(arg)
        tail = "".join(f", {x}" for x in extras)
        if not fn.cxx_body:
            return [f'{INDENT}m.def("{fn.name}", &{fn.cxx_name}{tail});']
        # A body, for a function whose C++ is assembled rather than named.
        # `gc_stats` reads five counters out of gc.h and hands back one
        # dict; there is no upstream function with that shape to point at.
        #
        # The opening spells the return type, as `_lambda_head` does for
        # a factory body: one rule for both kinds of body.
        body = [f"{INDENT * 2}{ln}".rstrip()
                for ln in fn.cxx_body.strip().splitlines()]
        return [f'{INDENT}m.def("{fn.name}", {self._lambda_head(fn)}', *body,
                f"{INDENT}}}{tail});"]

    def free_functions(self, fns: Sequence[ir.FunctionModel]) -> str:
        """Every free binding, in one function the module can call.

        The same seam a class gets. nanopynix's NB_MODULE already calls
        `nanopynix_bind_store(store)` and friends, so generated free
        functions arrive the same way hand-written ones do."""
        body: list[str] = []
        for fn in fns:
            body += self.free_function(fn)
        return "\n".join(["static void bind_functions(nb::module_ &m) {",
                           *body, "}"]) + "\n"

    def _converts_bytes(self, classes: Sequence[ir.ClassModel]) -> bool:
        """Whether any accessor here answers bytes, so a unit needs BYTES."""
        return any(m.returns is not None and m.returns.cxx in BYTES_SPELLINGS
                   for cls in classes for m in cls.bound)

    def _errors_used(self, classes: Sequence[ir.ClassModel],
                     functions: Sequence[ir.FunctionModel]) -> bool:
        """Whether this unit names a declared EXCEPTION class anywhere.

        One site is enough. A unit that answers with an exception has to
        look the Python class up by module and name, and the module name
        is not a literal any declaration may write - huggorm#63 is the
        day a stale copy of it turned every nix error into a
        RuntimeError. So the emitter states it once per unit that needs
        it, and the declaration's body reads it by name."""
        for _, t in _sites(classes, functions):
            if (t is not None and not t.required.origin
                    and t.required.kind == "error"):
                return True
        return False

    def module(self, classes: Sequence[ir.ClassModel],
               functions: Sequence[ir.FunctionModel],
               exported: Sequence[ir.FunctionModel],
               errors: str = "",
               error_headers: Sequence[str] = (),
               package: str = "") -> str:
        """One translation unit: the includes, then a bind function each.

        Several classes, not one. A declaration file owns a module and
        may declare more than one class in it - `decl/store.py` declares
        three - and a nanobind extension is one translation unit, so the
        file and the unit are the same grain."""
        head = [*self.includes(classes, functions, error_headers),
                *[f'#include "{records_include(m, package)}"'
                  for m in self.records_named(classes, functions)], "",
                "namespace nb = nanobind;",
                "using namespace nb::literals;", ""]
        if errors and self._errors_used(classes, functions):
            # Where `huggorm::as_error` looks a class up. Emitted, never
            # written: the same string the catch chain is given, from the
            # same derivation, so a renamed declaration moves both.
            head += ["namespace huggorm {", "",
                     "/** Where this build put the exception classes. */",
                     f'constexpr const char * errors_module = "{errors}";', "",
                     "}  // namespace huggorm", ""]
        if _crosses_container(classes):
            head += [*CONTAINERS.strip().splitlines(), ""]
        if self._converts_bytes(classes):
            head += [*BYTES.strip().splitlines(), ""]
        # `as_tuple`, for a value whose hash covers a list part. In the
        # unit, not in `records_header`, because a class that binds a real
        # Nix type needs the helper just the same: `pathinfo.cpp` has no
        # record.
        unions = self._unions_used(classes, functions)
        vocabularies = self._vocabularies_used(classes, functions)
        if any(_lists(cls) for cls in classes) or unions or vocabularies:
            head += [f"namespace {NAMESPACE} {{", ""]
            if any(_lists(cls) for cls in classes):
                head += [*HASHABLE.strip().splitlines(), ""]
            for v in vocabularies:
                head += words_conversion(v)
                if not v.decl.parsed_by:
                    head += words_from_word(v)
            for u in unions:
                head += (self.bare_check(u) if _is_bare(u)
                         else self.conversions(u))
            head += [f"}}  // namespace {NAMESPACE}", ""]
        # The casters come AFTER the conversions and outside the
        # namespace: each one calls a conversion by name, and a
        # specialisation has to live in nanobind's own namespace.
        wrapped = [u for u in unions if not _is_bare(u)]
        if wrapped:
            head += ["namespace nanobind::detail {", ""]
            for u in wrapped:
                head += self.caster(u)
            head += ["}  // namespace nanobind::detail", ""]
        out = "\n".join(head) + "\n" + "\n".join(
            self.bind_function(cls) for cls in classes)
        return out + ("\n" + self.free_functions(exported)
                      if exported else "")


def imports(unit: ir.ModuleModel, model: ir.Model) -> list[str]:
    """The other extensions whose types this one names.

    nanobind keeps ONE type registry for the whole process, so a
    `nix::StorePath` bound in `path` is the same Python class when
    `store` returns one - but only if `path` has been imported, and
    an extension cannot rely on a caller to have done that.

    So the module imports what it needs, and the list is derived: a
    class is visible only because the declaration imported it, and it
    needs binding only if it has C++ behind it or is a record the
    emitter declares. A vocabulary is filtered out here, which is why
    importing `HashAlgorithm` costs nothing."""
    return sorted({c.module for name in unit.visible
                   if (c := model.classes.get(name)) is not None
                   and (c.decl.cxx or c.is_value) and c.module != unit.name})


def extension(unit: ir.ModuleModel, dotted: str, model: ir.Model,
              chain: list[str] | None = None,
              errors: str = "",
              error_headers: Sequence[str] = ()) -> str:
    """One whole extension module: includes, bindings, entry point.

    `module` stops at the `bind_<name>` functions because that is the
    seam a project with a hand-written NB_MODULE needs. This goes the
    last step and writes the NB_MODULE too, which is what a module
    with nothing hand-written about it requires.

    The two are one line apart on purpose. A project adopting this
    gradually keeps its own entry point and calls the generated bind
    functions; a project that has finished takes this.

    `dotted` is where the build puts the module - `path` on its own,
    or `huggorm_bindings.path` inside a package. It is the one fact
    here no declaration carries, and it is what turns a sibling
    declaration's name into an import a running interpreter can
    follow."""
    classes = unit.bindable()
    package = dotted.rpartition(".")[0]
    reached = [f'{INDENT}nb::module_::import_("'
               f'{f"{package}." if package else ""}{stem}");'
               for stem in imports(unit, model)]
    translators = [translator(fn, chain) for fn in unit.translators]
    # Only a unit that HAS a translator catches anything, so only that
    # unit needs the headers behind the chain.
    return "\n".join([
        Emitter(model, unit).module(
            classes, unit.functions, unit.exported,
            errors,
            error_headers if translators else (), package),
        *translators,
        f"NB_MODULE({dotted.rpartition('.')[2]}, m) {{",
        # The declaration file's own docstring, which is the only
        # description of this module anyone wrote. Without it
        # `help(huggorm_bindings.path)` answers with nothing.
        *([f'{INDENT}m.doc() = "{_doc(unit.doc)}";'] if _doc(unit.doc) else []),
        # A startup hook goes ahead of everything, imports included -
        # an import runs another module's initialisation, and a
        # library that demands initialisation is entitled to it
        # before any of its code runs. libstore does not raise when
        # it has not been initialised; it aborts the process.
        *[f"{INDENT}{fn.cxx_name}();" for fn in unit.startup],
        # Then the other modules: a signature naming a type from one
        # of them is built as the binding is defined, so the class has
        # to already be registered.
        *reached,
        *[f"{INDENT}register_{fn.name.lstrip('_')}();"
          for fn in unit.translators],
        *[f"{INDENT}bind_{cls.name.lower()}(m);" for cls in classes],
        *([f"{INDENT}bind_functions(m);"] if unit.exported else []),
        "}",
        "",
    ])


def translator(fn: ir.FunctionModel, chain: list[str] | None = None) -> str:
    """The module's exception translator, registered once.

    ONCE for the module, and it runs for any binding in it. The
    Cython route named `except +translate_nix_error` on every method
    in the pxd, so the hook ran per call; this registers it in one
    place. The C++ is shared rather than emitted:
    `errors.hpp` maps nix::BadStorePath onto
    huggorm_bindings.errors.BadStorePath and strips libstore's
    terminal escapes.

    Which C++ - or whether there is any - is the declaration's to
    say. A library that throws plain std::exception needs none:
    nanobind already maps that to RuntimeError, which is what the
    mock relies on.

    `chain` is the catch chain, emitted from `decl/errors.py`. It
    replaces a call into hand-written C++, and the ORDER is the part
    that matters: C++ takes the first catch that matches, so a base
    listed before its subclass swallows it. Python inheritance
    already says which is which."""
    body = (chr(10).join(chain) if chain
            else f"{INDENT * 4}{fn.cxx_name}();")
    return f"""
static void register_{fn.name.lstrip("_")}() {{
    nb::register_exception_translator(
        [](const std::exception_ptr &p, void *) {{
            try {{
                std::rethrow_exception(p);
            }} catch (...) {{
                // Sets the Python error from inside catch(...),
                // which is where the exception is still live.
{body}
            }}
        }});
}}
"""


def census(cls: ir.ClassModel) -> dict[str, int]:
    """How much of this class the declaration derived, and how much a
    person wrote.

    Printed on every build, because a hatch nobody measures becomes
    the place the real code lives. The ratio is the honest measure of
    a binding: StorePath derives whole and hatches nothing;
    PathInfo renders a hash, a content address and a set of
    signatures, which are decisions rather than bindings, and it says
    so with a body each."""
    derived = hatched = hatch_lines = 0
    # A written `_from_parts` is a hatch like any other, and the one
    # most worth counting: it is the half of the wire that stopped
    # being true by construction when the value bound a real type.
    for m in (*cls.bound, *filter(None, (cls.from_parts,))):
        if m.cxx_body:
            hatched += 1
            hatch_lines += len(m.cxx_body.strip().splitlines())
        else:
            derived += 1
    if cls.init is not None:
        derived += 1
    derived += len(_value_semantics(cls)) and sum(
        1 for name, _, fact in COMPARISONS
        if {"value": cls.decl.compare == "cxx",
            "order": cls.decl.compare == "cxx" and cls.decl.order}[fact])
    return {"derived": derived, "hatched": hatched,
            "hatch_lines": hatch_lines}


if __name__ == "__main__":
    import sys

    from huggorm_gen.cppgen.generate import declared_model

    # A declaration file of the corpus, by module name: `path`, `store`.
    whole = declared_model()
    for name in sys.argv[1:]:
        unit = whole.module(name)
        emit = Emitter(whole, unit)
        for cls in unit.bindable():
            print(emit.module([cls], (), ()) if len(unit.classes) == 1
                  else emit.bind_function(cls))
            c = census(cls)
            total = c["derived"] + c["hatched"]
            print(f"// {cls.name}: {c['derived']}/{total} derived, "
                  f"{c['hatched']} through the hatch "
                  f"({c['hatch_lines']} lines of C++)", file=sys.stderr)
