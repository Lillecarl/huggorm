# Add lint + typecheck to the toolchain

The project has no ruff/mypy/pyright setup and no C++ static analysis;
the 2026-08-23 review found dead code, duplicate definitions and
type leaks that linters would have flagged.

Fix: add ruff (generator + fake-library-python), mypy or pyright for
the non-generated modules (generated code excluded), and clang-tidy or
at least -Wall fixes pass for the mock. Wire into a `nix run` check
and CI-able script.
