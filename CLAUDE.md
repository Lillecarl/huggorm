# Scratchpad
use .scratchpad as the scratchpad directory which is gitignored and easily accessible to be inspected by the user.

# Prose
Write prose according to ASD-STE100

# Anchoring
When you're uncertain about a librarys features or how to use it, anchor yourself by reading it's source code.
```bash
nix build --no-link --print-out-paths  --file . pkgs.$package.src # this can be used to fetch the source of dependencies you're working with
```
