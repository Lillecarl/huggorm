# Scratchpad
use .scratchpad as the scratchpad directory which is gitignored and easily accessible to be inspected by the user.

# Prose
Write prose according to ASD-STE100

# Anchoring
When you're uncertain about a librarys features or how to use it, anchor yourself by reading it's source code.
```bash
nix build --no-link --print-out-paths  --file . pkgs.$package.src # this can be used to fetch the source of dependencies you're working with
```

# Codegen
A goal of this repo is to derive as much code as possible from the declarations automatically through codegen. This reduces maintenance burden and ensures correctness.
Avoid hand-typing things that can be derived from a declaration, if it can't be derived from a declaration: Investigate if we can carry more in the declaration to facilitate codegen.
The declarations are in `cythonix-idl/src/cythonix_idl/decl/`. They are never executed: `read.py` parses them with `ast.parse`, and the emitters write the nanobind C++, the manifest entry, the type stub and the enum module from what they say.
