# Goals

Three, in priority order. Each carries the check that catches it being
cheated - because the codegen goal was already written here, and was
cheated anyway.

## 1. Correctness, measured against Nix

Be close to Nix. When unsure what Nix does, read Nix's source. Never
guess, never trust a memory of it.

- A binding may not be more permissive than the C++ it binds.
- Prove a gate by BREAKING it. A gate that has never failed has not
  been shown to test anything.

## 2. No hand-written C++ mapping. None.

`cythonix-idl/src/cythonix_idl/decl/` is the source. Everything else
is emitted from it: the nanobind C++, the manifest, the sync API, the
async API, the RPC API, the type stubs, the enums.

**A MAPPING is any C++ that says "this Python name means that C++
call".** An accessor, a constructor, a type test, an enum-to-string
table, a conversion. Every one of these is generated. There is no
budget for hand-writing them and no line count that makes it
acceptable - the answer to "the declaration cannot say this yet" is
to teach the declaration, and that is the task.

**A HELPER is infrastructure the generated code USES.** A GC root
over a foreign collector, thread registration a library exposes no
API for, an owner whose member ORDER is the fact. These are allowed,
and they exist to make the codegen simpler rather than to stand in
for it.

The test is who calls it. Generated code calls a helper. A mapping IS
the generated code, and if it is in `_cpp/*.hpp` it is in the wrong
file.

The build prints the `_cpp` line count. It is not a budget to spend.

**ASK BEFORE WRITING ANY C++ THAT THE CODEGEN DID NOT WRITE.** Every
line of it needs the user's explicit approval, in advance, per
occasion. Not "I will note it in the commit" and not "I will write a
task for deriving it later" - those are what happened while
`_cpp/eval.hpp` grew from 108 lines to 417, and every one of those
lines looked reasonable on its own.

Show what the line does, say why a declaration cannot carry it, and
wait. A "no" means the answer is to teach the declaration.

## 3. Maintainability, which is why 2 exists

One source, many outputs. A fact stated twice will disagree once.

- Derive, do not restate. A rule applied identically in twelve places
  belongs in the emitter, not in twelve places.
- Comments say WHY, and name the alternative that was rejected.
- Record decisions in `tasks/`, including the ones that turned out
  wrong.

When 1 and 2 conflict, 1 wins - and the conflict is a task.

# Not a goal yet

**Speed.** Doing the right thing comes first. Fix a pathology when it
is found, and do not trade a derived mapping for a hand-written fast
one.

# Scratchpad
use .scratchpad as the scratchpad directory which is gitignored and easily accessible to be inspected by the user.

# Prose
Write prose according to ASD-STE100

# Anchoring
When you're uncertain about a librarys features or how to use it, anchor yourself by reading it's source code.
```bash
nix build --no-link --print-out-paths  --file . pkgs.$package.src # this can be used to fetch the source of dependencies you're working with
```

# How the codegen works
The WHY is under Goals, above. This is the mechanism.
The declarations are in `cythonix-idl/src/cythonix_idl/decl/`, one file per Nix class, named after that class's header. `read.py` reads each one twice - it IMPORTS it, so Python resolves any `NIX_VERSION` branch, and it parses it with `ast.parse` for everything the import throws away. No body ever runs, so C++ written in a body is dead text the reader lifts out. The emitters then write the nanobind C++, the manifest entry, the type stub and the enum module from what the declaration says.
