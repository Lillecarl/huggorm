"""The Nix surface, declared. One document per Nix class.

Nothing in `decl/` is written for a particular output. A declaration
says what a Nix class IS - its methods, their types, the C++ each one
binds, the guards a tagged union needs - and an emitter decides what
that means for C++, for a stub, for an async wrapper or for the wire.

That is why the lists below live HERE and not with an emitter. Which
declarations exist, and which of them owns a compiled module, is a
fact about this set of documents. An emitter is handed the set; it
does not own it. Two backends read these same lists, and neither may
have its own idea of what is declared.

The documents are both imported and parsed. Importing them resolves
any version branch the way Python would; parsing them keeps
everything the import throws away, such as a docstring's position or
a C++ body written as a string. No body ever runs.
"""

import pathlib

# Where the documents are. Every reader opens them through this or
# through `declaration()`, so none of them reconstructs the path.
DECLARATIONS = pathlib.Path(__file__).resolve().parent / "decl"

# Declarations that own a WHOLE module, through nanobind. Each emits
# one C++ translation unit and there is no hand-written source for it
# at all - no pyx, no pxd, no shim header.
#
# The `.pyx` route these took is gone. Every workaround it needed
# went with it: a store path crossed as its base name and was parsed
# back, absence was the empty string, a set became a vector of
# strings, and a bound object came back as an owning raw pointer.
# nanobind casts all four, so the declaration stopped carrying them.
# A LIST, not a mapping of module to files, and the difference will
# matter one day. One declaration owns one module because one Nix
# header owns one class: `path-info.hh` is `pathinfo.py` is
# `huggorm_bindings.pathinfo`. The mapping becomes real the first
# time one HEADER's classes want separate declaration files -
# `store-api.hh` the day StoreLocation earns its own, or a genuinely
# multi-class header. Until then it would be machinery with no
# second case to keep it honest.
NANOBIND = (
    "path.py",
    "hash.py",
    "signature.py",
    "content_address.py",
    "derived_path.py",
    "realisation.py",
    "pathinfo.py",
    "store.py",
    "eval.py",
)

# Vocabularies. A StrEnum whose members ARE the strings a Nix parser
# takes, so there is no C++ and nothing to compile - the emitted
# module is plain Python and the build writes it whole, the way it
# writes a NANOBIND entry.
VOCABULARIES = (
    "words.py",
)

# The exception hierarchy. One declaration, two outputs: the Python
# module a caller catches, and the C++ catch chain that raises it.
ERRORS = "errors.py"


def declaration(name: str) -> str:
    """One declaration by name, as a path its reader can open.

    Named through here rather than found relative to a caller's own
    file, because there are several callers in several directories
    and each one guessing its way back to `decl/` is another chance
    to guess wrong."""
    return str(DECLARATIONS / (name if name.endswith(".py")
                               else f"{name}.py"))
