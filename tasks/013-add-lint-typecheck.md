# Add lint + typecheck to the toolchain

The project has no ruff/mypy/pyright setup and no C++ static analysis;
the 2026-08-23 review found dead code, duplicate definitions and
type leaks that linters would have flagged.

Fix: add ruff (generator + fake-library-python), mypy or pyright for
the non-generated modules (generated code excluded), and clang-tidy or
at least -Wall fixes pass for the mock. Wire into a `nix run` check
and CI-able script.

## Update 2026-08-25

Structural gates now exist where a linter cannot reach, all failing
the build:

- emitted modules must import exactly the names they use (this is what
  caught the sync Derivation arriving unused in every wrapper);
- emitted modules and classes must carry a real docstring;
- no module above the bindings may name a manifest class in a string
  literal (test_remote);
- the emitter-runtime symbol contract (011);
- the wire contract: policy, fields, helpers and threading must agree.

The 2026-08-25 review still found by hand what a linter would have
flagged in a second: a dead _PRIMITIVES table in pxd.py, a dead `hide`
parameter, a dead `import asyncio` inside a function, unused imports
in custom.py. That is the argument for actually adding ruff.

Remaining, unchanged: ruff over generator + fake-library-python
(generated package excluded - the smoke gates cover it), mypy or
pyright over the hand-written modules, and a -Wall/clang-tidy pass
over the mock. The generated wrappers are now typed honestly enough
for mypy to be worth pointing at consumers (see 017).
