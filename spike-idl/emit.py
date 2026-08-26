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

from declare import Decl
from read import Class, Method, Type, read

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

def _wire_fields(decl: Decl) -> str:
    inner = ", ".join(f'("{f.name}", "{f.type}")' for f in decl.fields)
    return f"({inner},)" if len(decl.fields) == 1 else f"({inner})"


def _accessor(m: Method, blocking: bool) -> list[str]:
    """One bound method: marshal in, call, marshal out.

    The copy rule lives HERE rather than in the declaration. The
    declaration says nix::StorePath::to_string returns a string_view;
    that a view must not outlive its owner is a fact about this
    boundary, so the emitter owns it and every view leaves as a copy
    without any declaration having to remember."""
    # `m.name`, not the C++ spelling. The pxd alias IS the mapping -
    # `hash_part "hashPart"` means Cython code says hash_part - so
    # calling the C++ name here would not compile.
    assert m.ret is not None
    args = ", ".join(n for n, _ in m.params)
    typed = "".join(f", {_py_type(t)} {n}" for n, t in m.params)
    lines = [f"{INDENT}def {m.name}(self{typed}) -> {_py_type(m.ret)}:"]
    lines += _doc(m.doc, 2)
    call = f"self._get().{m.name}({args})"
    if blocking or m.blocks:
        # A call that can wait releases the GIL around itself, so the
        # rest of the process keeps running while it does.
        lines += [f"{INDENT * 2}cdef {m.ret.cxx.spelling} out",
                  f"{INDENT * 2}with nogil:",
                  f"{INDENT * 3}out = {call}"]
        result = "out"
    else:
        result = call
    if m.ret.cxx.copy == "view":
        lines.append(f"{INDENT * 2}return _view({result})")
    elif m.ret.cxx.spelling == "string":
        lines.append(f"{INDENT * 2}return {result}.decode('utf-8')")
    else:
        lines.append(f"{INDENT * 2}return {result}")
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
        body.append(f"{INDENT}_wire_fields = {_wire_fields(decl)}")
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
