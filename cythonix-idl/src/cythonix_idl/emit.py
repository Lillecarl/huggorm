"""
Declaration -> Cython.

Reads through `read.py`, which PARSES rather than imports, so nothing
in a declaration ever runs. What arrives here is a `read.Class`: names,
docstrings, and C++ facts already resolved.

Emits three files, because Cython needs three: the C++ declarations
(`c_<name>.pxd`), our own cdef class's fields (`<name>.pxd`, so a
sibling module can reach them), and the binding (`<name>.pyx`).

## Text, not ast.unparse

emitter.py in the real generator builds Python with `ast` and renders
it with `ast.unparse`, which makes it impossible to emit code that
does not parse. That is not available here: a .pyx is not Python.
`cdef class`, `cdef string c_name`, `except NULL` and `with nogil:`
have no ast node, and Cython ships a parser but no unparser.

So this builds lines. The discipline it keeps instead is that every
line comes from a small named builder, and nothing is written by
concatenating a caller's string into a template.

## What it refuses

A shape it cannot emit stops with a reason. It does not guess, and it
does not emit something close - a binding that is subtly wrong costs
more than one that does not exist.
"""

import inspect
import pathlib
import sys

from cythonix_idl.declare import Decl
from cythonix_idl.read import Class, Method, Module, Type, read

INDENT = "    "


def _doc(text: str, level: int) -> list[str]:
    """One docstring, re-indented to where it is being written.

    read.py keeps docstrings RAW, at whatever indent the declaration
    wrote them. Emitting them straight would carry that indent into a
    file whose body sits somewhere else, so this normalises with
    cleandoc and re-indents to where it is being written."""
    if not text:
        return []
    pad = INDENT * level
    lines = inspect.cleandoc(text).splitlines()
    out = [f'{pad}"""{lines[0]}']
    for line in lines[1:]:
        out.append(f"{pad}{line}".rstrip())
    out[-1] += '"""'
    return out


def _py_type(t: Type) -> str:
    """How a declared type is spelled in a pyx signature.

    Not the C++ spelling: `bint` is Cython's own bool and reads as one
    in an annotation, while std::string and string_view are both `str`
    by the time a caller sees them. A type with no C++ behind it - a
    produced value's field - is already Python and says so itself."""
    if t.cxx is None:
        return "bint" if t.python == "bool" else t.python
    return "bint" if t.cxx.spelling == "bint" else "str"


# -- c_<name>.pxd ---------------------------------------------------------

def c_pxd(cls: Class, cname: str) -> str:
    decl = cls.decl
    out = [
        "# cython: language_level=3",
        f"# Declaration of {decl.cxx}, from {decl.header}.",
        "#",
        "# GENERATED from the declaration module - do not edit.",
        "",
        "from libcpp.string cimport string",
        "from libcpp.string_view cimport string_view",
        "",
        "# Every `except +translate_nix_error` below names this. A bare",
        "# `except +` would map a nix exception onto RuntimeError and",
        "# leave libstore's terminal escape codes in the message.",
        'cdef extern from "cythonix_bindings/_cpp/errors.hpp" namespace "cythonix" nogil:',
        f"{INDENT}cdef void translate_nix_error()",
        "",
        f'cdef extern from "{decl.header}" nogil:',
        f'{INDENT}cdef cppclass {cname} "{decl.cxx}":',
    ]
    body = [f"{cname}(const {cname} & other)"]
    if cls.ctor is not None:
        args = ", ".join(f"{t.cxx.spelling} {n}" for n, t in cls.ctor.params)
        body.append(f"{cname}({args}) except +translate_nix_error")
    for m in cls.methods:
        if m.ret is None:
            raise TypeError(f"{m.name}: a declared method must state a "
                            f"return type")
        alias = f' "{m.cxx_name}"' if m.cxx_name else ""
        args = ", ".join(f"{t.cxx.spelling} {n}" for n, t in m.params)
        body.append(
            f"{m.ret.cxx.spelling} {m.name}{alias}({args}) "
            f"except +translate_nix_error")
    if decl.compare == "cxx":
        body.append(f"bint operator==(const {cname} & other)")
        if decl.order:
            body.append(f"bint operator<(const {cname} & other)")
    out += [f"{INDENT * 2}{line}" for line in body]
    return "\n".join(out) + "\n"


# -- <name>.pxd -----------------------------------------------------------

def our_pxd(cls: Class, cname: str, module: str) -> str:
    return "\n".join([
        "# cython: language_level=3",
        f"# So another module can reach a {cls.name}'s pointer.",
        "#",
        "# GENERATED from the declaration module - do not edit.",
        "",
        f"from cythonix_bindings.c_{module} cimport {cname}",
        "",
        "",
        f"cdef class {cls.name}:",
        f"{INDENT}cdef {cname}* _ptr",
        "",
        f"{INDENT}cdef inline {cname}* _get(self) except NULL",
    ]) + "\n"


# -- <name>.pyx -----------------------------------------------------------

def _wire_fields(fields: list[tuple[str, str]], level: int) -> list[str]:
    """The `_wire_fields` tuple, one field per line past the first.

    A single field reads better inline - path.pyx has one and says so
    on one line. Nine do not, so PathInfo's go one to a line, which is
    how the hand-written file already writes them."""
    pad = INDENT * level
    if len(fields) == 1:
        n, t = fields[0]
        return [f'{pad}_wire_fields = (("{n}", "{t}"),)']
    out = [f"{pad}_wire_fields = ("]
    out += [f'{pad}{INDENT}("{n}", "{t}"),' for n, t in fields]
    out.append(f"{pad})")
    return out


def waits(cls: Class, m: Method) -> bool:
    """Whether this call releases the GIL around itself.

    `blocking` is a property of the CLASS's calls in general, and a
    general rule has exceptions both ways: `@blocks` on a class that
    mostly does not, `@instant` on a class that mostly does. The
    per-method fact wins, and `@instant` wins over `@blocks` because
    a method carrying both is a declaration to fix, not to guess at."""
    if m.instant:
        return False
    return cls.decl.blocking or m.blocks


def _marshal_in(m: Method) -> tuple[list[str], list[str]]:
    """Python arguments to C++ ones, as (declarations, call arguments).

    Everything a `nogil` block touches has to be a C local before the
    block opens, because inside it there is no interpreter to reach a
    Python object through. That is why this hoists rather than
    spelling the conversions inside the call: `path.encode('utf-8')`
    is Python work, and it must already be done."""
    decls, args = [], []
    for name, t in m.params:
        if t.bound:
            # Another declared class. Its pointer is what C++ wants,
            # and `_get()` is the guard that says so when it is NULL.
            # The parameter is already typed in the signature, so the
            # call needs no second local to reach `_get` through.
            decls.append(f"{INDENT * 2}cdef C{t.python}* c_{name} = "
                         f"{name}._get()")
            args.append(f"deref(c_{name})")
        elif t.cxx is not None and t.cxx.spelling == "string":
            decls.append(
                f"{INDENT * 2}cdef string c_{name} = {name}.encode('utf-8')")
            args.append(f"c_{name}")
        else:
            args.append(name)
    return decls, args


def _marshal_out(t: Type, expr: str) -> str:
    """One C++ result as the Python object the signature promised."""
    if t.cxx is None:
        raise TypeError(f"no C++ spelling for a return of {t.python}")
    if t.cxx.copy == "view":
        return f"_view({expr})"
    if t.cxx.spelling == "string":
        return f"{expr}.decode('utf-8')"
    return expr


def _accessor(m: Method, blocking: bool, cls: Class | None = None) -> list[str]:
    """One bound method: marshal in, call, marshal out.

    The copy rule lives HERE rather than in the declaration. The
    declaration says nix::StorePath::to_string returns a string_view;
    that a view must not outlive its owner is a fact about this
    boundary, so the emitter owns it and every view leaves as a copy
    without any declaration having to remember.

    Two call shapes, and which one is used is DERIVED. A method with
    no `@cxx_body` is a method on the C++ class, and the call reads
    `self._get().name(...)`. One with a body is a free function in the
    shim header this emitter also writes, and the call passes the
    object as its first argument. Neither the declaration nor a
    reader has to say which; the presence of a body decides."""
    # `m.name`, not the C++ spelling. The pxd alias IS the mapping -
    # `hash_part "hashPart"` means Cython code says hash_part - so
    # calling the C++ name here would not compile.
    assert m.ret is not None
    # Cython-style for a method, Python-style for a shim, and the
    # difference is not taste. The codegen above backfills a
    # parameter's type from the pxd declaration of the C++ method; a
    # shim has no such declaration, so `str path` reaches it as Any
    # and `path: str` does not. Derived from whether there is a body,
    # like the call shape itself.
    if m.cxx_body:
        typed = "".join(f", {n}: {_py_type(t)}" for n, t in m.params)
    else:
        typed = "".join(f", {_py_type(t)} {n}" for n, t in m.params)
    lines = [f"{INDENT}def {m.name}(self{typed}) -> {_py_type(m.ret)}:"]
    lines += _doc(m.doc, 2)
    hoists, args = _marshal_in(m)
    waiting = waits(cls, m) if cls is not None else (blocking or m.blocks)
    # `self._get()` is an attribute reach through a Python object, so
    # it cannot happen inside `nogil` - there is no interpreter in
    # there to reach through. A shim needs the pointer as its first
    # argument in every case; a method needs it hoisted only when the
    # call releases the GIL. Getting this wrong is a compile error
    # rather than a silent one, but only for a class that blocks -
    # which is why StorePath never found it.
    cname = f"C{cls.name}" if cls is not None else "C"
    if waiting:
        # First, before the parameters. The receiver is the one local
        # every hoisting call has, so putting it first makes the block
        # read the same shape in every method.
        hoists.insert(0, f"{INDENT * 2}cdef {cname}* c_self = self._get()")
        target = "deref(c_self)" if m.cxx_body else "c_self"
    else:
        # No nogil block, so nothing has to be a C local first and the
        # pointer is reached where it is used.
        target = "deref(self._get())" if m.cxx_body else "self._get()"
    if m.cxx_body:
        call = f"{m.name}(" + ", ".join([target, *args]) + ")"
    else:
        call = f"{target}.{m.name}(" + ", ".join(args) + ")"
    lines += hoists
    if waiting:
        # A call that can wait releases the GIL around itself, so the
        # rest of the process keeps running while it does.
        lines += [f"{INDENT * 2}cdef {m.ret.cxx.spelling} out",
                  f"{INDENT * 2}with nogil:",
                  f"{INDENT * 3}out = {call}"]
        result = "out"
    else:
        result = call
    lines.append(f"{INDENT * 2}return {_marshal_out(m.ret, result)}")
    return lines


def pyx(cls: Class, cname: str, module: str, doc: str) -> str:
    name, decl = cls.name, cls.decl
    ctor_params = cls.ctor.params if cls.ctor is not None else ()
    needs_view = any(m.ret is not None and m.ret.cxx is not None
                     and m.ret.cxx.copy == "view"
                     for m in cls.methods)
    needs_string = bool(ctor_params) or any(
        t.cxx.spelling == "string" for m in cls.methods for _, t in m.params)

    head = ["# cython: language_level=3", "# cython: annotation_typing=False"]
    head += [f"# {line}".rstrip() for line in doc.strip().splitlines()]
    head += ["#", "# GENERATED from the declaration module - do not edit.", ""]
    if decl.order:
        head += ["import functools", ""]
    if decl.wire == "value":
        head += ["from cythonix_bindings import _value", ""]
    if decl.compare == "cxx":
        head.append("from cython.operator cimport dereference as deref")
    if needs_string:
        head.append("from libcpp.string cimport string")
    if needs_view:
        head.append("from libcpp.string_view cimport string_view")
    head += ["", f"from cythonix_bindings.c_{module} cimport {cname}", "", ""]

    if needs_view:
        head += [
            "cdef inline str _view(string_view v):",
            f'{INDENT}"""A view into C++ storage, copied into a Python str.',
            "",
            f"{INDENT}Holding one past the object it points into is a dangling",
            f"{INDENT}pointer rather than an exception, so nothing that leaves",
            f'{INDENT}this file is ever a view."""',
            f"{INDENT}return v.data()[:v.size()].decode('utf-8')",
            "", ""]

    body = []
    if decl.order:
        body.append("@functools.total_ordering")
    body.append(f"cdef class {name}:")
    body += _doc(cls.doc, 1)
    body.append("")
    for marker, value in (("_threading", f'"{decl.threading}"'),
                          ("_binds", f'"{cname}"'),
                          ("_blocking", str(decl.blocking)),
                          ("_wire", f'"{decl.wire}"')):
        if value in ('""', "None"):
            continue
        body.append(f"{INDENT}{marker} = {value}")
    if decl.fields:
        body += _wire_fields([(f.name, f.type) for f in decl.fields], 1)
    body.append("")

    if cls.ctor is not None:
        # `str base_name`, not `string base_name`: a pyx signature is
        # the Python surface, and the C++ spelling belongs in the pxd.
        args = ", ".join(f"{_py_type(t)} {n}" for n, t in ctor_params)
        body.append(f"{INDENT}def __init__(self, {args}):")
        body += _doc(cls.ctor.doc, 2)
        # __init__ and not __cinit__: __cinit__ runs on every __new__,
        # the argument-less one a copy needs included, so it cannot
        # also be where construction happens.
        for n, t in ctor_params:
            if t.cxx.spelling == "string":
                body.append(
                    f"{INDENT * 2}cdef string c_{n} = {n}.encode('utf-8')")
        built = ", ".join(f"c_{n}" if t.cxx.spelling == "string" else n
                          for n, t in ctor_params)
        body += [f"{INDENT * 2}self._ptr = new {cname}({built})", ""]

    body += [
        f"{INDENT}def __dealloc__(self):",
        f"{INDENT * 2}del self._ptr",
        "",
        f"{INDENT}cdef inline {cname}* _get(self) except NULL:",
        f'{INDENT * 2}"""The underlying object, or a clear error.',
        "",
        f"{INDENT * 2}Reachable only through __new__ without __init__, which",
        f"{INDENT * 2}is what a copy does before it assigns. Without this the",
        f'{INDENT * 2}next accessor dereferences NULL."""',
        f"{INDENT * 2}if self._ptr is NULL:",
        f'{INDENT * 3}raise ValueError("this {name} was never constructed")',
        f"{INDENT * 2}return self._ptr",
        "",
        f"{INDENT}def __copy__(self):",
        f"{INDENT * 2}cdef {name} c = {name}.__new__({name})",
        f"{INDENT * 2}c._ptr = new {cname}(self._get()[0])",
        f"{INDENT * 2}return c",
        "",
        f"{INDENT}def __deepcopy__(self, memo):",
        f"{INDENT * 2}# A bound value is immutable: deep copy == copy.",
        f"{INDENT * 2}return self.__copy__()",
        "",
    ]

    if decl.wire == "value":
        body += _value_semantics(name, decl)

    for m in cls.methods:
        body += _accessor(m, decl.blocking)
        body.append("")

    if decl.fields:
        body += _round_trip(cls)
    for source in decl.custom.values():
        body += [f"{INDENT}{line}".rstrip() for line in source.splitlines()]
        body.append("")
    return "\n".join(head + body).rstrip() + "\n"


def _value_semantics(name: str, decl: Decl) -> list[str]:
    """What every wire value owes a caller.

    The emitter owns the SHAPE and the declaration owns the two
    choices inside it: whether equality is C++'s or the parts', and
    which accessor str() answers with."""
    out = [
        f"{INDENT}# A value compares, hashes and prints as the thing it IS.",
        f"{INDENT}# hash and repr come from the same _wire_fields the wire",
        f"{INDENT}# uses; see _value.py.",
    ]
    if decl.compare == "cxx":
        out += [
            f"{INDENT}def __eq__(self, other):",
            f"{INDENT * 2}if not isinstance(other, {name}):",
            f"{INDENT * 3}return NotImplemented",
            f"{INDENT * 2}return deref(self._get()) == deref((<{name}>other)._get())",
            "",
        ]
        if decl.order:
            out += [
                f"{INDENT}def __lt__(self, other):",
                f"{INDENT * 2}if not isinstance(other, {name}):",
                f"{INDENT * 3}return NotImplemented",
                f"{INDENT * 2}return deref(self._get()) < deref((<{name}>other)._get())",
                "",
            ]
    else:
        out += [f"{INDENT}def __eq__(self, other):",
                f"{INDENT * 2}return _value.eq(self, other)", ""]
    out += [
        f"{INDENT}def __hash__(self):",
        f"{INDENT * 2}return _value.hash_(self)",
        "",
        f"{INDENT}def __repr__(self):",
        f"{INDENT * 2}return _value.repr_(self)",
        "",
    ]
    if decl.text:
        out += [f"{INDENT}def __str__(self):",
                f"{INDENT * 2}return self.{decl.text}()", ""]
    return out


def _round_trip(cls: Class) -> list[str]:
    """_from_parts and _parts, derived from the field declarations.

    Derivable only when the parts map onto the constructor. A value
    that is PRODUCED - built by another object, with an __init__ that
    raises - needs the __new__-and-assign form instead, and this
    refuses rather than emitting the wrong one."""
    decl = cls.decl
    ctor_params = cls.ctor.params if cls.ctor is not None else ()
    if len(ctor_params) != len(decl.fields):
        raise TypeError(
            f"{cls.name}: {len(decl.fields)} declared field(s) and "
            f"{len(ctor_params)} constructor parameter(s). A produced value "
            f"needs the __new__-and-assign form, which this emitter does not "
            f"write yet.")
    # Typed, like the constructor's: _from_parts takes exactly what
    # the constructor takes, so it states the same types.
    args = ", ".join(f"{_py_type(t)} {f.name}"
                     for f, (_, t) in zip(decl.fields, ctor_params,
                                          strict=True))
    reads = ", ".join(f"self.{f.read}()" for f in decl.fields)
    return [
        f"{INDENT}@classmethod",
        f"{INDENT}def _from_parts(cls, {args}):",
        f'{INDENT * 2}"""Wire-deserialization helper (private). The',
        f'{INDENT * 2}constructor already validates, so this is it."""',
        f"{INDENT * 2}return {cls.name}("
        + ", ".join(f.name for f in decl.fields) + ")",
        "",
        f"{INDENT}def _parts(self):",
        f'{INDENT * 2}"""Wire-serialization helper (private): one value per',
        f'{INDENT * 2}_wire_fields entry, in order."""',
        f"{INDENT * 2}return ({reads},)" if len(decl.fields) == 1
        else f"{INDENT * 2}return ({reads})",
    ]


def produced_pyx(cls: Class) -> list[str]:
    """A value that holds no C++ at all.

    The shape is different from a bound class rather than a variation
    on it, and every difference follows from one fact: the object that
    made this flattened one, so what is left is Python slots.

    No pointer, so no `_get()` NULL guard, no `__dealloc__`, no
    `__copy__` - a slot is already a Python reference and copying one
    is what assignment does. No `_binds`, because there is no C++
    declaration to name. And no constructor: `__init__` raises with
    the sentence `@produced(by=...)` supplied, so a caller who guesses
    wrong is told where to look instead of getting a TypeError with no
    address in it."""
    decl = cls.decl
    name = cls.name
    fields = [(m.name, m.ret) for m in cls.methods if m.ret is not None]

    out = [f"cdef class {name}:"]
    out += _doc(cls.doc, 1)
    out.append("")
    for marker, value in (("_threading", f'"{decl.threading}"'),
                          ("_blocking", str(decl.blocking)),
                          ("_wire", f'"{decl.wire}"'),
                          ("_produced", "True")):
        out.append(f"{INDENT}{marker} = {value}")
    # Derived from the accessors, not declared: every accessor IS a
    # field of a produced value, so the name is the accessor's name
    # and the wire type is what it returns.
    out += _wire_fields([(n, t.wire) for n, t in fields], 1)
    out.append("")
    for n, _ in fields:
        # `object`, not a typed slot: these hold whatever the store
        # handed over, StorePath and list included.
        out.append(f"{INDENT}cdef object _{n}")
    out += [
        "",
        f"{INDENT}def __init__(self):",
        f"{INDENT * 2}raise TypeError(",
        f'{INDENT * 3}"{name} objects come from {decl.built_by}, not from a '
        f'constructor")',
        "",
    ]
    out += _value_semantics(name, decl)
    for m in cls.methods:
        out.append(f"{INDENT}def {m.name}(self) -> {_py_type(m.ret)}:")
        out += _doc(m.doc, 2)
        out += [f"{INDENT * 2}return self._{m.name}", ""]

    args = ", ".join(n for n, _ in fields)
    out += [
        f"{INDENT}@classmethod",
        f"{INDENT}def _from_parts(cls, {args}):",
        f'{INDENT * 2}"""Wire-deserialization helper (private, never '
        f'surfaced)."""',
        # __new__ without __init__, because __init__ refuses. This is
        # the only place that may build one, and it is private.
        f"{INDENT * 2}cdef {name} out = {name}.__new__({name})",
    ]
    out += [f"{INDENT * 2}out._{n} = {n}" for n, _ in fields]
    out += [
        f"{INDENT * 2}return out",
        "",
        f"{INDENT}def _parts(self):",
        f'{INDENT * 2}"""Wire-serialization helper (private): one value per',
        f'{INDENT * 2}_wire_fields entry, in order."""',
        f"{INDENT * 2}return (" + ", ".join(f"self._{n}" for n, _ in fields)
        + ("," if len(fields) == 1 else "") + ")",
    ]
    return out


def produced_pxd(cls: Class) -> list[str]:
    """The slots, so a sibling module can fill them.

    A bound class's pxd publishes a pointer. This publishes the
    object slots, because that is all a produced value has - and
    something else has to assign them."""
    out = [f"cdef class {cls.name}:"]
    out += [f"{INDENT}cdef object _{m.name}"
            for m in cls.methods if m.ret is not None]
    return out


# The C++ types a shim signature is spelled in. A declaration says
# `Str`, which is `std::string` at the boundary and `const
# std::string &` as a parameter - one fact, two positions, and the
# emitter owns the difference because it is about C++ rather than
# about Nix.
CXX_RETURN = {"string": "std::string", "string_view": "std::string_view",
              "bint": "bool"}
CXX_PARAM = {"string": "const std::string &", "string_view": "std::string_view",
             "bint": "bool"}


def _shim_signature(cls: Class, m: Method) -> str:
    """One shim, as C++ declares it."""
    assert m.ret is not None and m.ret.cxx is not None
    ret = CXX_RETURN.get(m.ret.cxx.spelling, m.ret.cxx.spelling)
    # Non-const, always. A const reference would document that a call
    # only reads, but the declaration does not carry that fact and a
    # non-const reference binds to the object this binding holds in
    # every case - so guessing const would be a compile error waiting
    # for the first method that mutates.
    args = [f"{cls.decl.cxx} & s"]
    for name, t in m.params:
        if t.bound:
            args.append(f"const nix::{t.python} & {name}")
        elif t.cxx is not None:
            args.append(f"{CXX_PARAM.get(t.cxx.spelling, t.cxx.spelling)} "
                        f"{name}")
        else:
            raise TypeError(f"{m.name}: parameter {name} has no C++ spelling")
    return f"inline {ret} {m.name}({', '.join(args)})"


def shim_hpp(cls: Class, module: str, doc: str) -> str:
    """The C++ this module's Cython cannot say, written from the
    declaration.

    `_cpp/README.md` lists three reasons a shim exists, and all three
    are mechanical: a by-value return of a type with no default
    constructor, a member reached through a reference member, and a
    template Cython has no declaration for. None of them is about
    Nix, and none of them is a decision - which is what makes them
    emittable.

    The body itself still comes from the declaration, through
    `@cxx_body`. That is not a gap: `store.config.getHumanReadableURI()`
    is the ONE line a reader has to know, and everything around it -
    the signature, the reference parameter, the namespace, the
    include list - is derived.

    One docstring, not two. The hand-written header and the
    hand-written pyx each carried their own prose about the same
    method, which is the same fact written twice. The declaration's
    docstring lands in both."""
    out = [
        "#pragma once",
        f"// C++ that {module}.pyx needs and a pxd cannot say.",
        "//",
        "// GENERATED from the declaration module - do not edit.",
        "//",
    ]
    out += [f"// {line}".rstrip()
            for line in inspect.cleandoc(doc).splitlines()]
    out += ["", "#include <string>", ""]
    for path in sorted({cls.decl.header} - {""}):
        out.append(f'#include "{path}"')
    out += ["", "namespace cythonix {", ""]
    for m in cls.methods:
        if not m.cxx_body:
            continue
        out.append("/**")
        # cleandoc, like _doc: read.py keeps a docstring at whatever
        # indent the declaration wrote it, and a C++ comment is not
        # the place to carry Python's indentation.
        out += [f" * {line}".rstrip()
                for line in inspect.cleandoc(m.doc).splitlines()]
        out.append(" */")
        out.append(_shim_signature(cls, m))
        out.append("{")
        out += [f"{INDENT}{line}".rstrip()
                for line in m.cxx_body.strip().splitlines()]
        out += ["}", ""]
    out += ["}  // namespace cythonix", ""]
    return "\n".join(out)


def produced_pxi(mod: Module, source: str) -> str:
    """Every produced value in one declaration, as a Cython include.

    A `.pxi` is a TEXTUAL include: `include "x.pxi"` splices the file
    into the module that names it, before anything else looks at the
    tree. So a class emitted here lands in the including module - same
    `__module__`, same module-private `cdef` helpers around it,
    nothing re-exported and nothing to import.

    That is what makes it the right shape for a module the emitter has
    taken over only PART of. `store.pyx` holds Store, which is
    abstract in C++ and opened by a URI, so the emitter cannot write
    it. It also held PathInfo and StoreLocation, which are plain
    values and which the emitter can. An include splits those two
    groups without moving a class between modules, which would change
    a name every downstream reader already uses.

    The alternative was to splice text into the .pyx at a marker. That
    is the same job done by a script that must not lose a line, and it
    was tried first: it compiled and left a test failing. A compiler
    feature that already means "paste this here" is not worth
    reimplementing."""
    out = [
        "# GENERATED. Do not edit.",
        "#",
        f"# Emitted from {source} by spike-idl/emit.py, and included",
        "# textually by the module that names it. Every class here is a",
        "# produced value: the store built it, so it holds Python slots",
        "# and no C++ at all.",
        "",
    ]
    for cls in mod.classes:
        if not cls.is_value:
            continue
        out += produced_pyx(cls)
        out.append("")
    return "\n".join(out) + "\n"


def cython_files(cls: Class, module: str, doc: str) -> dict[str, str]:
    """The three files one declared class needs."""
    cname = "C" + cls.name
    return {
        f"c_{module}.pxd": c_pxd(cls, cname),
        f"{module}.pxd": our_pxd(cls, cname, module),
        f"{module}.pyx": pyx(cls, cname, module, doc),
    }


def emit(path: str, out_dir: str) -> dict[str, str]:
    mod = read(path)
    if len(mod.classes) != 1:
        raise TypeError(
            f"{path}: this spike emits exactly one class per module, got "
            f"{len(mod.classes)}")
    cls = mod.classes[0]
    files = cython_files(cls, mod.name, mod.doc)
    target = pathlib.Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (target / fname).write_text(text)
    custom_lines = sum(len(s.splitlines()) for s in cls.decl.custom.values())
    print(f"emitted {len(files)} file(s) for {cls.name}; "
          f"{custom_lines} line(s) came through the custom hatch")
    return files


if __name__ == "__main__":
    emit(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".")
