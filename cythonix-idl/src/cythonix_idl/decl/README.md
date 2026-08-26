# Declarations

One file per bound area. **Nothing here is ever executed** - the
emitters read these with `ast.parse`, so a declaration can name a C++
type this machine has never compiled.

That is also why they sit in their own directory. A declaration and
the machinery that reads it are different kinds of file, and a linter
can only be told so once: `ruff.toml` exempts
`cythonix-idl/src/cythonix_idl/decl/*.py` from the three rules that
fire on the things which make a file a declaration rather than a
program (`F821`, `UP037`, `PIE790`). The machinery next door is
exempt from nothing.

Before this directory existed, every new declaration needed its own
line in `ruff.toml`. A rule you must remember to extend is a rule
that eventually is not extended.
