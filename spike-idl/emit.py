"""
Declaration -> Cython. The spike (tasks/050's end state).

Reads a declaration module by IMPORT and inspect - never by parsing -
so the source of truth is a real Python object with real annotations,
and a typo in it is an ImportError rather than a silently unmatched
regex.

Emits three files, because Cython needs three: the C++ declarations
(`c_<name>.pxd`), our own cdef class's fields (`<name>.pxd`, so a
sibling module can reach them), and the binding (`<name>.pyx`).

## Text, not ast.unparse

emitter.py builds Python with `ast` and renders it with
`ast.unparse`, which is what makes it impossible to emit code that
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

import importlib
import inspect
import sys
from typing import Any, get_args, get_origin

from declare import Cxx, Decl

INDENT = "    "


def _doc(obj: object, level: int) -> list[str]:
    """One docstring, re-indented to where it is being written.

    inspect.getdoc dedents, which is right for reading and wrong for
    emitting: the continuation lines would land flush against the
    left margin of a file whose body is indented."""
    text = inspect.getdoc(obj)
    if not text:
        return []
    pad = INDENT * level
    lines = text.splitlines()
    out = [f'{pad}"""{lines[0]}']
    for line in lines[1:]:
        out.append(f"{pad}{line}".rstrip())
    out[-1] += '"""'
    return out


def cxx_of(annotation: Any) -> Cxx:
    """The C++ fact attached to a declared type, or a refusal.

    Every type a binding names has one. A bare `str` would leave the
    emitter guessing between std::string and string_view, and those
    differ by whether the boundary owes it a copy."""
    if get_origin(annotation) is None:
        raise TypeError(
            f"{annotation!r} carries no C++ spelling. Declare it through an "
            f"Annotated alias in declare.py rather than as a bare type.")
    for meta in get_args(annotation)[1:]:
        if isinstance(meta, Cxx):
            return meta
    raise TypeError(f"{annotation!r} is Annotated but not with Cxx")


def _py_type(c: Cxx) -> str:
    """How a declared type is spelled in a pyx signature.

    Not the C++ spelling: `bint` is Cython's own bool and reads as one
    in an annotation, while std::string and string_view are both `str`
    by the time a caller sees them."""
    return "bint" if c.spelling == "bint" else "str"


def methods(cls: type) -> list[tuple[str, Any]]:
    """The declared methods, in DEFINITION order.

    `cls.__dict__` keeps it and `inspect.getmembers` sorts it away.
    Order is not cosmetic here: it is the order a reader of the
    declaration sees, and the emitted file should read the same way."""
    return [(n, v) for n, v in cls.__dict__.items()
            if inspect.isfunction(v) and not n.startswith("_")]


def _sig(fn: Any) -> tuple[list[tuple[str, Cxx]], Cxx | None]:
    hints = inspect.get_annotations(fn, eval_str=False)
    params = [(n, cxx_of(hints[n]))
              for n in list(inspect.signature(fn).parameters)[1:]]
    ret = hints.get("return")
    return params, None if ret in (None, type(None)) else cxx_of(ret)


# -- c_<name>.pxd ---------------------------------------------------------

def c_pxd(cls: type, decl: Decl, cname: str) -> str:
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
    ctor = cls.__dict__.get("__init__")
    if ctor is not None:
        params, _ = _sig(ctor)
        args = ", ".join(f"{c.spelling} {n}" for n, c in params)
        body.append(f"{cname}({args}) except +translate_nix_error")
    for name, fn in methods(cls):
        params, ret = _sig(fn)
        if ret is None:
            raise TypeError(f"{name}: a declared method must state a return type")
        spelled = getattr(fn, "_cxx_name", None)
        alias = f' "{spelled}"' if spelled else ""
        args = ", ".join(f"{c.spelling} {n}" for n, c in params)
        body.append(
            f"{ret.spelling} {name}{alias}({args}) "
            f"except +translate_nix_error")
    if decl.compare == "cxx":
        body.append(f"bint operator==(const {cname} & other)")
        if decl.order:
            body.append(f"bint operator<(const {cname} & other)")
    out += [f"{INDENT * 2}{line}" for line in body]
    return "\n".join(out) + "\n"


# -- <name>.pxd -----------------------------------------------------------

def our_pxd(cls: type, cname: str, module: str) -> str:
    return "\n".join([
        "# cython: language_level=3",
        f"# So another module can reach a {cls.__name__}'s pointer.",
        "#",
        "# GENERATED from the declaration module - do not edit.",
        "",
        f"from cythonix_bindings.c_{module} cimport {cname}",
        "",
        "",
        f"cdef class {cls.__name__}:",
        f"{INDENT}cdef {cname}* _ptr",
        "",
        f"{INDENT}cdef inline {cname}* _get(self) except NULL",
    ]) + "\n"


# -- <name>.pyx -----------------------------------------------------------

def _wire_fields(decl: Decl) -> str:
    inner = ", ".join(f'("{f.name}", "{f.type}")' for f in decl.fields)
    return f"({inner},)" if len(decl.fields) == 1 else f"({inner})"


def _accessor(name: str, fn: Any, ret: Cxx, params: list[tuple[str, Cxx]],
              cname: str, blocking: bool) -> list[str]:
    """One bound method: marshal in, call, marshal out.

    The copy rule lives HERE rather than in the declaration. The
    declaration says nix::StorePath::to_string returns a string_view;
    that a view must not outlive its owner is a fact about this
    boundary, so the emitter owns it and every view leaves as a copy
    without any declaration having to remember."""
    # `name`, not the C++ spelling. The pxd alias IS the mapping -
    # `hash_part "hashPart"` means Cython code says hash_part - so
    # calling the C++ name here would not compile.
    args = ", ".join(n for n, _ in params)
    py_ret = _py_type(ret)
    typed = "".join(f", {_py_type(c)} {n}" for n, c in params)
    lines = [f"{INDENT}def {name}(self{typed}) -> {py_ret}:"]
    lines += _doc(fn, 2)
    call = f"self._get().{name}({args})"
    if blocking or getattr(fn, "_blocks", False):
        # A call that can wait releases the GIL around itself, so the
        # rest of the process keeps running while it does.
        lines += [f"{INDENT * 2}cdef {ret.spelling} out",
                  f"{INDENT * 2}with nogil:",
                  f"{INDENT * 3}out = {call}"]
        result = "out"
    else:
        result = call
    if ret.copy == "view":
        lines.append(f"{INDENT * 2}return _view({result})")
    elif ret.spelling == "string":
        lines.append(f"{INDENT * 2}return {result}.decode('utf-8')")
    else:
        lines.append(f"{INDENT * 2}return {result}")
    return lines


def pyx(cls: type, decl: Decl, cname: str, module: str, doc: str) -> str:
    name = cls.__name__
    all_sigs = [_sig(fn) for _, fn in methods(cls)]
    ctor = cls.__dict__.get("__init__")
    ctor_params, _ = _sig(ctor) if ctor is not None else ([], None)
    needs_view = any(r and r.copy == "view" for _, r in all_sigs)
    needs_string = bool(ctor_params) or any(
        c.spelling == "string" for ps, _ in all_sigs for _, c in ps)

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
    body.append(f"cdef class {cls.__name__}:")
    body += _doc(cls, 1)
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

    if ctor is not None:
        # `str base_name`, not `string base_name`: a pyx signature is
        # the Python surface, and the C++ spelling belongs in the pxd.
        args = ", ".join(f"{_py_type(c)} {n}" for n, c in ctor_params)
        body.append(f"{INDENT}def __init__(self, {args}):")
        body += _doc(ctor, 2)
        # __init__ and not __cinit__: __cinit__ runs on every __new__,
        # the argument-less one a copy needs included, so it cannot
        # also be where construction happens.
        for n, c in ctor_params:
            if c.spelling == "string":
                body.append(f"{INDENT * 2}cdef string c_{n} = {n}.encode('utf-8')")
        built = ", ".join(f"c_{n}" if c.spelling == "string" else n
                          for n, c in ctor_params)
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
        body += _value_semantics(name, cname, decl)

    for mname, fn in methods(cls):
        params, ret = _sig(fn)
        body += _accessor(mname, fn, ret, params, cname, decl.blocking)
        body.append("")

    if decl.fields:
        body += _round_trip(name, decl, ctor_params)
    for source in decl.custom.values():
        body += [f"{INDENT}{line}".rstrip() for line in source.splitlines()]
        body.append("")
    return "\n".join(head + body).rstrip() + "\n"


def _value_semantics(name: str, cname: str, decl: Decl) -> list[str]:
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


def _round_trip(name: str, decl: Decl,
                ctor_params: list[tuple[str, Cxx]]) -> list[str]:
    """_from_parts and _parts, derived from the field declarations.

    Derivable only when the parts map onto the constructor. A value
    that is PRODUCED - built by another object, with an __init__ that
    raises - needs the __new__-and-assign form instead, and this
    refuses rather than emitting the wrong one."""
    if len(ctor_params) != len(decl.fields):
        raise TypeError(
            f"{name}: {len(decl.fields)} declared field(s) and "
            f"{len(ctor_params)} constructor parameter(s). A produced value "
            f"needs the __new__-and-assign form, which this emitter does not "
            f"write yet.")
    # Typed, like the constructor's: _from_parts takes exactly what
    # the constructor takes, so it states the same types.
    args = ", ".join(f"{_py_type(c)} {f.name}"
                     for f, (_, c) in zip(decl.fields, ctor_params, strict=True))
    reads = ", ".join(f"self.{f.read}()" for f in decl.fields)
    return [
        f"{INDENT}@classmethod",
        f"{INDENT}def _from_parts(cls, {args}):",
        f'{INDENT * 2}"""Wire-deserialization helper (private). The',
        f'{INDENT * 2}constructor already validates, so this is it."""',
        f"{INDENT * 2}return {name}("
        + ", ".join(f.name for f in decl.fields) + ")",
        "",
        f"{INDENT}def _parts(self):",
        f'{INDENT * 2}"""Wire-serialization helper (private): one value per',
        f'{INDENT * 2}_wire_fields entry, in order."""',
        f"{INDENT * 2}return ({reads},)" if len(decl.fields) == 1
        else f"{INDENT * 2}return ({reads})",
    ]


def emit(module: str, out_dir: str) -> dict[str, str]:
    mod = importlib.import_module(module)
    classes = [v for v in vars(mod).values()
               if isinstance(v, type) and "_decl" in v.__dict__]
    if len(classes) != 1:
        raise TypeError(
            f"{module}: this spike emits exactly one class per module, got "
            f"{len(classes)}")
    cls = classes[0]
    decl: Decl = cls.__dict__["_decl"]
    cname = "C" + cls.__name__
    files = {
        f"c_{module}.pxd": c_pxd(cls, decl, cname),
        f"{module}.pxd": our_pxd(cls, cname, module),
        f"{module}.pyx": pyx(cls, decl, cname, module, mod.__doc__ or ""),
    }
    import pathlib
    target = pathlib.Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    for fname, text in files.items():
        (target / fname).write_text(text)
    custom_lines = sum(len(s.splitlines()) for s in decl.custom.values())
    print(f"emitted {len(files)} file(s) for {cls.__name__}; "
          f"{custom_lines} line(s) came through the custom hatch")
    return files


if __name__ == "__main__":
    emit(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ".")
