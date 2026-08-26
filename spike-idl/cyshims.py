"""
Plain-Python twins for the cimports a C++ binding names.

Cython's pure mode spells a cimport `from cython.cimports.X import Y`,
and `Cython.Shadow` makes that work at RUN time for exactly one
package: it registers `cython.cimports.libc.math` as the stdlib
`math`. Everything else falls to `CythonCImports.__getattr__`, which
refuses every dunder BEFORE its import_module fallback - so Python's
own import machinery cannot resolve the submodule and the file that
was supposed to be ordinary Python raises `AttributeError: __spec__`.

The fix is Cython's own idiom, applied wider: a module already in
`sys.modules` is found without the mock being asked at all. So this
registers twins for the libcpp declarations a binding actually names.

Nothing here runs when the file is COMPILED. Cython resolves
`cython.cimports.libcpp.memory` against its own `.pxd` and never
executes this. These exist so the same source is readable by an
importer - a generator reflecting the declaration, a typechecker, a
reader running `python -i`.
"""

import sys
import types

# Imported for its SIDE EFFECT, before anything below runs. Shadow
# publishes its own `cython.cimports` mock at import time and would
# overwrite the replacement installed here if it landed second.
import cython as _cython  # noqa: F401


class _Cpp:
    """A C++ name standing in for itself.

    Subscriptable because a template is (`shared_ptr[CThing]`),
    callable because a factory is (`make_shared[CThing](x)`), and
    usable as an annotation because a declaration is. It answers
    nothing, and nothing asks: a pure-mode file is imported to be
    READ, not to run its bodies."""

    def __init__(self, name: str, args: tuple[object, ...] = ()) -> None:
        self.name = name
        self.args = args

    def __getitem__(self, item: object) -> "_Cpp":
        # A NEW one, carrying the parameter. Returning self would make
        # `shared_ptr[CStorePath]` indistinguishable from `shared_ptr`,
        # and the parameter is exactly what a generator needs to write
        # the declaration back out.
        return _Cpp(self.name, item if isinstance(item, tuple) else (item,))

    def __call__(self, *args: object, **kwargs: object) -> "_Cpp":
        return self

    def spelling(self) -> str:
        """This type as C++ spells it, parameters included."""
        if not self.args:
            return self.name
        inner = ", ".join(
            a.spelling() if isinstance(a, _Cpp) else getattr(a, "__name__", str(a))
            for a in self.args)
        return f"{self.name}[{inner}]"

    def __repr__(self) -> str:
        return f"<c++ {self.spelling()}>"


def _install_cimports_package() -> None:
    """Make `cython.cimports.X` resolve to the plain module `X`.

    Shadow's own mock nearly does this - `CythonCImports.__getattr__`
    ends in an `import_module` - but it refuses every dunder first, so
    Python's import machinery cannot even ask for `__spec__` and the
    submodule never resolves.

    A real ModuleType has a `__spec__`, so replacing the mock with one
    lets the machinery work, and PEP 562's module `__getattr__` does
    the fallback the mock intended. Every `c_<name>.py` becomes its
    own cimport twin with no line of its own."""
    pkg = types.ModuleType("cython.cimports")
    # EMPTY, and widened by declarations() below. A submodule search
    # walks __path__ as directories, so this is what makes
    # `cython.cimports.c_store` find the ordinary `c_store.py` beside
    # the binding - the whole trick: one declaration file, cimported
    # when compiled and imported when read.
    #
    # sys.path would work and is too wide: it would make any top-level
    # module reachable as a cimport twin, so a typo would resolve to
    # something unrelated instead of failing.
    pkg.__path__ = []  # type: ignore[attr-defined]

    # Keep what Shadow already published: libc.math is a real twin.
    for key, mod in list(sys.modules.items()):
        if key.startswith("cython.cimports.") and key.count(".") == 2:
            setattr(pkg, key.rsplit(".", 1)[1], mod)
    sys.modules["cython.cimports"] = pkg


_install_cimports_package()


def declarations(*directories: object) -> None:
    """Where the `c_<name>.py` twins live.

    Called by whatever is READING a pure-mode binding - a generator, a
    typechecker, a person at a prompt. Nothing calls it when the file
    is compiled: Cython resolves the cimport against the pxd and never
    runs a line of this."""
    pkg = sys.modules["cython.cimports"]
    for directory in directories:
        path = str(directory)
        if path not in pkg.__path__:  # type: ignore[attr-defined]
            pkg.__path__.append(path)  # type: ignore[attr-defined]


def register(package: str, **names: object) -> None:
    """Publish one cimport twin, the way Shadow publishes libc.math."""
    mod = types.ModuleType(package)
    for name, value in names.items():
        setattr(mod, name, value)
    sys.modules.setdefault(f"cython.cimports.{package}", mod)
    head = package.split(".")[0]
    sys.modules.setdefault(f"cython.cimports.{head}",
                           types.ModuleType(head))


def _cpp(*names: str) -> dict[str, _Cpp]:
    return {n: _Cpp(n) for n in names}


register("libcpp.memory", **_cpp("shared_ptr", "unique_ptr",
                                 "make_shared", "make_unique"))
register("libcpp.string", **_cpp("string"))
register("libcpp.string_view", **_cpp("string_view"))
register("libcpp.vector", **_cpp("vector"))


# `cython.operator` is the other one Shadow leaves out, and a C++
# binding names it the moment it dereferences a pointer. Same
# treatment: `cython` is a MODULE rather than a package, so a
# submodule of it only resolves if sys.modules already holds one.
def _identity(x: object) -> object:
    """What a dereference is, to a reader.

    Compiled, `deref(p)` is `*p`. Interpreted, nothing is being run -
    so the honest stand-in is the thing itself rather than a raise."""
    return x


_operator = types.ModuleType("cython.operator")
for _name in ("dereference", "address", "preincrement", "predecrement",
              "postincrement", "postdecrement"):
    setattr(_operator, _name, _identity)
sys.modules.setdefault("cython.operator", _operator)
