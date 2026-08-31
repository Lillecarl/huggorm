# Declarations

One file per Nix class, named after the header that class comes from.
`nix/store/path.hh` is `path.py`; `nix/store/path-info.hh` is
`pathinfo.py`.

Each is read **twice**: imported, and parsed. The import resolves any
`NIX_VERSION` branch, so nothing outside Python interprets a version
condition. The tree carries what the import throws away - the C++
inside a body, the order the methods are declared in.

**No body ever runs.** `def` defines; it does not call. So `Cxx(...)`
in a body is dead text the reader lifts out of the tree, and a
declaration can still name a C++ type this machine has never compiled.

That is also why they sit in their own directory. A declaration and
the machinery that reads it are different kinds of file, and a linter
can only be told so once: `ruff.toml` exempts
`huggorm-idl/src/huggorm_idl/decl/*.py` from the three rules that
fire on the things which make a file a declaration rather than a
program (`F821`, `UP037`, `PIE790`). The machinery next door is
exempt from nothing.

Before this directory existed, every new declaration needed its own
line in `ruff.toml`. A rule you must remember to extend is a rule
that eventually is not extended.
