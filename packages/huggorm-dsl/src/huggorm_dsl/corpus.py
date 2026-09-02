"""A SET of declarations, read once.

`read.py` answers about ONE file. Every emitter wants the whole set,
so every emitter grew the same three lines:

    for name in NANOBIND:
        mod = read(str(DECLARATIONS / name))
        ...

Five functions in `cppgen/generate.py` opened that way, and each then
re-derived what it needed. That is a loop repeated, not a fact
derived, and it costs three things.

It costs correctness of scope. `Module.known` answers "what can THIS
file name", which is the right question for emitting one translation
unit and the wrong one for any question about the set - such as
"which declared class is handed back by some other declaration".
Every caller that wanted the wider answer built its own.

It costs work. `read()` follows a declaration's imports and reads
each one, so `words.py` is read again for every declaration that
names a vocabulary. Nine declarations, read five times over, with
their imports re-read inside each - for a set that never changes
during a build.

And it costs the reader. A person looking for "which declarations
exist" found a `for` loop, five times, and had to check that all five
looped over the same list.

So this is the missing type rather than a convenience. A Corpus is
handed the set, reads each document at most once, and answers the
questions an emitter asks about the whole of it.

It lives here, with the language, because "a set of declarations" is
a fact about the language rather than about any one output. WHICH
documents make up the set is a different fact, and that one stays
with the documents: `huggorm_decl` builds the instance from its own
lists.
"""

import ast
import pathlib
from types import ModuleType

from huggorm_dsl.read import (
    Class,
    Method,
    Module,
    collecting,
    load,
    read,
)
from huggorm_dsl.read import resolved as resolve_tree


class Corpus:
    """Every declaration in one set, read at most once each.

    Three groups, because the build treats them differently and
    nothing else about them differs. A NANOBIND declaration owns a
    compiled module. A VOCABULARY is plain Python with no C++ behind
    it. The ERRORS declaration is neither: it emits a Python module
    and a C++ catch chain, and nothing reads it as a `Module`.
    """

    def __init__(self, root: pathlib.Path, *,
                 nanobind: tuple[str, ...] = (),
                 vocabularies: tuple[str, ...] = (),
                 errors: str = "") -> None:
        self.root = pathlib.Path(root)
        self._nanobind = nanobind
        self._vocabularies = vocabularies
        self._errors = errors
        # Keyed by file NAME, not by resolved path. Every name here
        # comes from one of the three lists above, and they name
        # files in one directory.
        self._read: dict[str, Module] = {}
        self._parsed: dict[str, ast.Module] = {}
        self._chosen: dict[str, ast.Module] = {}

    # -- one document at a time ------------------------------------

    def path(self, name: str) -> pathlib.Path:
        """One declaration's file, by name.

        Through here rather than joined by each caller, so no caller
        reconstructs the directory."""
        return self.root / (name if name.endswith(".py") else f"{name}.py")

    def module(self, name: str) -> Module:
        """One declaration, read.

        Cached. `read()` imports the file and follows its imports, so
        it is the expensive call in the build, and the answer cannot
        change while a build runs."""
        if name not in self._read:
            self._read[name] = read(str(self.path(name)))
        return self._read[name]

    def tree(self, name: str) -> ast.Module:
        """One declaration's syntax tree.

        Some emitters read the tree directly rather than the `Module`
        - `pyerrors` walks the exception hierarchy and `pyenum` wants
        a docstring's exact text. They parsed it themselves, which
        meant `errors.py` was parsed twice in one function."""
        if name not in self._parsed:
            self._parsed[name] = ast.parse(self.path(name).read_text())
        return self._parsed[name]

    def resolved(self, name: str) -> ast.Module:
        """One declaration's tree, with its version branches chosen.

        What `tree` gives, minus the arm this build does not have.
        Both are here because they answer different questions: `tree`
        is the document as written, which is what a docstring's exact
        text and a marker scan want, and this is the document as this
        build reads it.

        For an emitter whose OUTPUT is the tree. `pyerrors` copies an
        exception declaration through, so a branch it does not resolve
        reaches the emitted module unresolved (tasks/073)."""
        if name not in self._chosen:
            self._chosen[name] = resolve_tree(str(self.path(name)))
        return self._chosen[name]

    def imported(self, name: str) -> ModuleType | None:
        """One declaration, as the module Python built from it.

        The reading a tree cannot give: Python resolved every base
        chain and every inherited attribute while executing the file.
        A reader that walks the tree for those recomputes what the
        interpreter already knows, and gets it subtly wrong the first
        time a hierarchy is three deep.

        `read()` imports the same file for the same reason - to learn
        which definitions a `NIX_VERSION` branch kept - and `load` is
        cached by path, so this shares that one execution rather than
        running the decorators a second time.

        `None` when the file will not import, which is a legitimate
        answer: `read()` falls back to the tree alone and so must a
        caller here."""
        return load(str(self.path(name)))

    # -- the three groups ------------------------------------------

    def read_all(self) -> None:
        """Read every declaration, reporting every refusal at once.

        A generator calls this BEFORE it emits anything. Reading is
        where a declaration is refused, and a refusal per run means a
        person fixing three mistakes waits for three builds.

        Here rather than in a generator because this is the object
        that knows what "every declaration" is - and both generators
        would otherwise carry the same list.

        Idempotent and free after the first call: every read below is
        cached, so this populates the caches and later access pays
        nothing."""
        with collecting():
            for name in (*self._nanobind, *self._vocabularies):
                self.module(name)
            if self._errors:
                self.tree(self._errors)
                self.imported(self._errors)

    @property
    def nanobind(self) -> tuple[str, ...]:
        """The file name of each declaration that owns a module."""
        return self._nanobind

    @property
    def modules(self) -> tuple[Module, ...]:
        """Each declaration that owns a compiled module, in order.

        DECLARED order, not sorted. A later pass numbers a oneof's
        fields from it, so a reorder is a wire change."""
        return tuple(self.module(n) for n in self._nanobind)

    @property
    def module_names(self) -> tuple[str, ...]:
        """The compiled module each declaration owns, by module name.

        The stem, not the file name: `path.py` owns `path`. The build
        loops over these to know what to emit and what to compile,
        and `setup.py` reads the same list."""
        return tuple(m.name for m in self.modules)

    @property
    def vocabularies(self) -> tuple[str, ...]:
        """The file name of each vocabulary declaration.

        Names, like `nanobind`, rather than `Module`s like `modules`.
        The emitter that writes one wants the tree as well as the
        module, and both are keyed by name."""
        return self._vocabularies

    @property
    def errors(self) -> str:
        """The exception declaration's file name, or "" if there is none.

        A name rather than a `Module`, because nothing reads this one
        as a declaration of classes. Two emitters parse its tree."""
        return self._errors

    # -- the whole set ---------------------------------------------

    @property
    def classes(self) -> tuple[Class, ...]:
        """Every class any declaration in the set declares.

        Unions are not here. A union names other types and binds no
        C++ class of its own, which is the same split `Module` makes."""
        return tuple(c for m in self.modules for c in m.classes)

    @property
    def unions(self) -> tuple[Class, ...]:
        """Every SUM type the set declares."""
        return tuple(u for m in self.modules for u in m.unions)

    @property
    def functions(self) -> tuple[Method, ...]:
        """Every free function the set declares, startup hooks and
        translators included.

        `Module.exported` is the filter that removes the two that are
        not surface. It stays a filter rather than becoming the
        default here: the C++ emitter wants all three."""
        return tuple(f for m in self.modules for f in m.functions)

    @property
    def known(self) -> dict[str, Class]:
        """Every type the SET can name, by name.

        Wider than `Module.known`, and deliberately so. A module's
        answer is what that one translation unit may name, which is
        the right scope for emitting it. A question about the set -
        which class is handed back, which name is a union - needs the
        set's answer, and each caller used to build one.

        A vocabulary is in here too. It is declared in its own file
        and named by several bindings, so a lookup that missed it
        would answer "not a declared type" about a type the set
        declares."""
        out: dict[str, Class] = {}
        for mod in (*self.modules,
                    *(self.module(n) for n in self._vocabularies)):
            for cls in (*mod.classes, *mod.unions):
                out[cls.name] = cls
        return out
