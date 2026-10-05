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
#16 holds the detail and the lifecycle contract.

The binding exists because that service needs Nix in-process. The
codegen exists because that service needs six surfaces to agree, and a
fact stated six times disagrees once.

Three audiences, and the order is not a ranking - all three are real:
Carl's own Nix tooling, editor and direnv-style workflows that want a
warm evaluation behind them, and other people who want Nix from Python
as a library.

## Break it, if breaking it is better

Carl, 2026-09-05:

> Breaking compatibility is not frowned upon in this repo, it's
> encouraged if it helps improve the codebase. This repo is an
> elaborate spike, backwards compatibility is not in our terminology,
> the best possible thing forwards is the only goal.

So a published name, a wire field, a constructor, a signature - none
of them is a reason to keep a worse shape. If a change makes the
codebase better, make it.

**This line used to say the opposite.** It read "the third is what
makes a breaking change expensive", about the third audience above.
It is deleted rather than softened, because it was steering: it is
what turned `NixClient`'s asyncio spawn into a question for Carl
instead of a change to make (#35).

What DOES still stop and ask is unchanged and is a different thing: an
action that is irreversible outside this repository, and a choice
between options that lead to materially different work. Breaking an
interface is neither. Say what broke in the commit message, and go.

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

0. **The architecture, before anything else.** Carl, 2026-10-05:
   "it's highest priority that the architecture is solid". The work
   is a TYPED MODEL between the reader and the emitters (#29). The
   manifest is `dict[str, Any]` and types cross it as strings, so
   each emitter re-derives the same rules from strings and names,
   and the copies drift. Measured on one day: "is this a proxy" was
   an exact string match in three places that all missed `X | None`,
   and "is this private" was a leading-`_` test in five places that
   silently dropped `__call__`. A rule lives on the model once.
   Features wait for this.

   **Everything is on the table.** Carl, 2026-10-05: "if you find
   poor architecture you fix it". Standing mandate, no ask needed:
   - Poor architecture found anywhere -> fix it; reduce complexity.
   - The declaration DSL is not fixed. Change it when a simpler
     spelling removes emitter work.
   - Add decl TYPES where C++ needs a home to map to Python (as
     `Path = Annotated[pathlib.Path, Cxx("string"), Async(...)]`
     does): the type carries the C++ spelling, the Python spelling
     and any surface twin, so no emitter keeps a mapping table.
   - Prefer typed structure over strings at every stage, the
     runtime codec included.
   - Refactor gate: byte-identical output. A real bug fix gets its
     own commit and gate, with the output diff named.
1. A defect that can corrupt a value, or drop one silently. This
   repo's named failure mode is the SILENT SKIP - an emitter skips
   what it does not recognise, and a skip is indistinguishable from an
   absence. Seven found so far: #73, #75, #78, #82, #87, #88,
   #90.

   The seventh has a different shape and the same outcome. An emitter
   ERASED what a later reader needed: `pyerrors.module` stripped the
   `cxx` lines off the tree `corpus()` caches for the process, so a
   reader after it saw a declaration with no C++ in it and emitted a
   translator that catches nothing. So the rule is wider than
   "skips what it does not recognise" - it is anything that leaves an
   emitter with less than the declaration said, quietly.
2. Whatever the destination above needs next and does not have.
3. An issue that is outstanding and blocks nothing.

The open issues are the board.

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

**A HELPER NEEDS NO ASK. A MAPPING IS STILL BANNED.** Carl,
2026-09-05:

> Writing a bit of manual C++ is OK to "help" the codegen, of course
> there are things which won't be expressible in Python DSL and in
> these cases we write C++ that "assists" the DSL (manual C++ can be
> something the emitter can bind to pretty much).

So the line above is the whole rule, and it is the line that was
always there. Write the helper. Say in the commit what it does and
why a declaration cannot carry it.

**This used to say ASK BEFORE WRITING ANY C++, per occasion.** That
rule was written after `cpp/eval.hpp` grew from 108 lines to 417 by
accretion, and it over-corrected: it made a GC traversal slot - pure
infrastructure, no Python name resolving to it - into a question
instead of a fix. The thing that grew `eval.hpp` was mappings sneaking
in as "just one more line", and the MAPPING ban is what stops that.

Two tests, and both have to pass before you write it:

1. **Who calls it.** Generated code calls a helper. If a Python name
   resolves to it, it is a mapping and it is banned however small.
2. **Could a declaration say this?** If yes, teach the declaration -
   that is still the answer, and the helper is still the wrong file.

The best helpers are the ones the emitter BINDS TO: a table, a
symbol, a slot the generated `nb::class_` names. That keeps the
generated side the caller and the helper the thing called.

Still ask when the answer is a design choice rather than a shape -
an ownership model, a threading rule, a lifetime nobody has decided.
That is the "materially different work" test, not a C++ test.

## 3. Maintainability, which is why 2 exists

One source, many outputs. A fact stated twice will disagree once.

- Derive, do not restate. A rule applied identically in twelve places
  belongs in the emitter, not in twelve places.
- Comments say WHY, and name the alternative that was rejected.
- Record decisions in the issues, including the ones that turned out
  wrong.

When 1 and 2 conflict, 1 wins - and the conflict is an issue.

# anyio, not asyncio

Carl's rule, 2026-09-05:

> We should be using anyio primitives instead of asyncio to the
> greatest extent possible (preferably only). anyio's structured async
> model is good at preventing bugs.

So `anyio.Lock`, `anyio.sleep`, `anyio.Event`, `anyio.fail_after` and
`move_on_after`, and a TASK GROUP wherever a task is started. Never
`asyncio.create_task`, `asyncio.ensure_future`, `asyncio.gather`,
`asyncio.wait_for`, `asyncio.Lock` or `asyncio.sleep`.

The point is the structure, not the spelling. A task group OWNS its
children: it cannot lose one, it cannot leak one, and a failure in one
reaches the caller. `asyncio.create_task` holds a weak reference, so a
fire-and-forget task can be collected before it runs - which this repo
worked around by retaining a set of them by hand.

grpclib is asyncio-only and always will be, so the backend stays
asyncio. That is not a reason to write asyncio: anyio runs ON asyncio,
and everything above is available there.

**One exception, and it is measured.** The thread bridge in the
emitted runtime (`huggorm_gen/payload/runtime.py`) keeps
`loop.run_in_executor`. `anyio.to_thread.run_sync` runs on a SHARED,
CHURNING pool - `_asyncio.py` pops an idle worker off a deque and
expires any that idled past `MAX_IDLE_TIME` - so it cannot name a
thread. An `EvalState` is affine and must be touched from ONE thread,
and `_refuse_foreign` compares executor IDENTITY to enforce the
isolation Carl ruled on. #35 holds the reading and the
alternative that was rejected.

Anything new that needs a thread asks first. Anything else is anyio.

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
already had (#62, #68).

The exception is anything nanobind resolves at compile or run time
rather than in the text. A missing type_caster emits fine and
compiles fine, and fails a gate later (#67).

**The build's own reports are in the DERIVATION LOG, not on your
terminal.** A cached derivation prints nothing, and `nix run` does
not carry the stdout of what it built. So before claiming the build
does not report something, read the log:

    nix build --file . bindings-src --no-link --json > /tmp/d.json
    nix log $(python3 -c "import json;print(json.load(open('/tmp/d.json'))[0]['drvPath'])")

(`--json` and `--print-out-paths` together emit two documents, so
passing both and parsing the result as JSON fails. Written that way
here first, and it did not run.)

`census_cpp`'s `hand-written C++ in cpp/: N lines` lives there, and
so does the orphan list. Claimed absent three times in one session
and present every time (#91).

# Maintain the issues as you work

The tracker is GitHub issues on `Lillecarl/huggorm`. Issue #N is the
old `tasks/NNN` file for every N up to 105, so a `tasks/NNN` in a
commit message means #N. #106 holds the old board, `tasks/README.md`.
Refer to an issue as `#N` in Markdown, and as `huggorm#N` in code, in
comments and in error messages.

Goal 3 says to record decisions in the issues, including the ones that
turned out wrong. That is not a step at the end.

**Update the issue while the work happens.** Comment with the
measurement when you take it, not from memory afterwards. A number
recalled at the end is a number you did not check.

**Record what you got WRONG, and say what refuted it.** A rationale
that turned out false, a gate that turned out not to hold, a shape you
argued for and then measured against - none of that is anywhere else
in the repo. Two claims written into comments were refuted by the
compiler; both are recorded, and both would have been believed forever
otherwise.

**Close an issue from the commit that finishes it**, with `Closes #N`
on its own line in the body. A decision that ends without code gets a
closing comment that says what was decided.

**Open an issue for work you name and do not do.** A next step
described in a reply is lost when the session ends. If you would say
"this is worth doing next", it is worth an issue.

Every issue or comment body ends with `---` and then
`Assisted-By: <model>`.

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
