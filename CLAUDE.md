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
A goal of this repo is to derive code as possible from the Cython bindings automatically through codegen. This reduces maintenance burden and ensures correctness.
Avoid hand-typing things that can be derived from the bindings, if it can't be derived from the bindings: Investigate if we can extract more data from the bindings to facilitate codegen
