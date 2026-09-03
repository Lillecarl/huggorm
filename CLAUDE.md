# What this is for

huggorm binds Nix to Python, and generates the binding itself from a
declaration. One file per Nix class decides six surfaces: the C++
binding, the type stub, the manifest entry, the async wrapper, the RPC
client and the gRPC schema.

**The destination is an evaluation service, not a binding.** A binding
that opens a store and evaluates an expression is the floor. What this
is built toward is a service that outlives its callers: one
`EvalState` serving many connections over days, clients that detach
and later reclaim the same warm state, no re-evaluation of unchanged
input, every non-store file an evaluation touched under a watch, and
registered expressions evaluated eagerly in the background so a
user-triggered eval is already in progress or already done.

That is what the RPC layer is for. Not other languages - the
evaluator has to outlive the process that asked for it, and a socket
is how a later client finds the state an earlier one left warm.
`tasks/016` holds the detail and the lifecycle contract.

The binding exists because that service needs Nix in-process. The
codegen exists because that service needs six surfaces to agree, and a
fact stated six times disagrees once.

Three audiences, and the order is not a ranking - all three are real:
Carl's own Nix tooling, editor and direnv-style workflows that want a
warm evaluation behind them, and other people who want Nix from Python
as a library. The third is what makes a breaking change expensive.

## The goal reached

A second client claims a live `EvalState`, and re-evaluating unchanged
input does no re-evaluation.

That is the smallest thing that proves the service is real: it needs
the handle to outlive its creator, the state to still be warm, and the
evaluator to know that nothing it depends on has changed. Watched
files and background eager evaluation come after it, and 014's
transports after that.

## Picking the next thing

In order:

1. A defect that can corrupt a value, or drop one silently. This
   repo's named failure mode is the SILENT SKIP - an emitter skips
   what it does not recognise, and a skip is indistinguishable from an
   absence. Six found so far: `tasks/073`, `075`, `078`, `082`,
   `087`, `088`.
2. Whatever the destination above needs next and does not have.
3. A task that is outstanding and blocks nothing.

`tasks/README.md` is the board.

# Goals

Three, in priority order. They are how the work is done, not what it
is for - the destination is above. Each carries the check that catches
it being cheated, because the codegen goal was already written here,
and was cheated anyway.

## 1. Correctness, measured against Nix

Be close to Nix. When unsure what Nix does, read Nix's source. Never
guess, never trust a memory of it.

- A binding may not be more permissive than the C++ it binds.
- Prove a gate by BREAKING it. A gate that has never failed has not
  been shown to test anything.

## 2. No hand-written C++ mapping. None.

`packages/huggorm-decl/src/huggorm_decl/decl/` is the source. Everything else
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
the generated code, and if it is in `huggorm_decl/cpp/*.hpp` it is in the wrong
file.

The build prints the `huggorm_decl/cpp` line count. It is not a budget to spend.

**ASK BEFORE WRITING ANY C++ THAT THE CODEGEN DID NOT WRITE.** Every
line of it needs the user's explicit approval, in advance, per
occasion. Not "I will note it in the commit" and not "I will write a
task for deriving it later" - those are what happened while
`cpp/eval.hpp` grew from 108 lines to 417, and every one of those
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

# Build the cheap thing first

    nix build --file . bindings-src        seconds,  124 KB
    nix build --file . huggorm-bindings    minutes,  3.3 MB

Most emitter changes are proved by DIFFING the emitted C++ against
the previous store path. Compile only when the C++ changed shape.
1792 dead build outputs and 1.8 GB were sitting in the store when
this was measured, and the disk filling is a failure this repo has
already had (`tasks/062`, `tasks/068`).

The exception is anything nanobind resolves at compile or run time
rather than in the text. A missing type_caster emits fine and
compiles fine, and fails a gate later (`tasks/067`).

# Maintain `tasks/` as you work

Goal 3 says to record decisions in `tasks/`, including the ones that
turned out wrong. That is not a step at the end. `tasks/README.md`
holds the conventions; this is the part an agent forgets.

**Update the task while the work happens.** Write the measurement when
you take it, not from memory afterwards. A number recalled at the end
is a number you did not check.

**Record what you got WRONG, and say what refuted it.** A rationale
that turned out false, a gate that turned out not to hold, a shape you
argued for and then measured against - none of that is anywhere else
in the repo. Two of the last three tasks found a claim written into a
comment that the compiler refuted; both are recorded, and both would
have been believed forever otherwise.

**Three things must agree, and drift silently when they do not:**

1. the `.done` suffix on the filename,
2. the bold status word on the file's first line,
3. what `tasks/README.md` says about that number.

Change all three in the same commit. Check them before you stop:

```bash
cd tasks && for f in [0-9]*.md*; do
  case "$f" in *.done) a=done;; *) a=open;; esac
  w=$(sed -n 1,12p "$f" | grep -m1 -oE '\*\*(OPEN|DONE|MOSTLY DONE|PARKED|CLOSED)' | tr -d '*')
  case "$w" in DONE|CLOSED) b=done;; *) b=open;; esac
  [ "$a" = "$b" ] || echo "MISMATCH $f file=$a line=${w:-none}"
done
```

**Open a task for work you name and do not do.** A next step described
in a reply is lost when the session ends. If you would say "this is
worth doing next", it is worth a file.

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
The declarations are in `packages/huggorm-decl/src/huggorm_decl/decl/`, one file per Nix class, named after that class's header. `read.py` reads each one twice - it IMPORTS it, so Python resolves any `NIX_VERSION` branch, and it parses it with `ast.parse` for everything the import throws away. No body ever runs, so C++ written in a body is dead text the reader lifts out. The emitters then write the nanobind C++, the manifest entry, the type stub and the enum module from what the declaration says.
