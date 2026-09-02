# The manifest is a runtime interpreter, not an IR

**MOSTLY DONE.** `manifest.json` is not an intermediate between the
two codegen stages. It was a table that three hand-written modules
read on every call, and that made it the same pathology as
hand-written C++ mapping - in a different language, and better
hidden.

The file is DELETED. `tasks/065` is the fix and all four of its
phases landed: every reader calls `build_manifest()` instead, and the
tables the library needs are emitted into
`huggorm_generated/_policy.py` where a typechecker can see them.

One of the two front doors is DONE. `huggorm_bindings/__init__.py`
is emitted, and the whole package directory is empty in the checkout
now - see the last section but one.

What is left is `huggorm/__init__.py` - about thirty hand-written
re-export lines that track the declarations by hand. Smaller than the
manifest and independent of it, and it needs a decision rather than
an implementation: a generated file inside the hand-written package
would break the trick `nix run test` uses to put the tree's
`huggorm/` ahead of the store copy.

Deferred on purpose. Carl: *"Let's begin with a conservative
restructuring and renaming, once we're done with that we'll discuss
dumping the IR in favor of both AST and import introspection for both
C++ and Python generation."* The restructure is done. This is the
next conversation, written down so the findings survive it.

## Where it came from

Carl, on reading the layout: *"I don't understand why we emit the
JSON at all, my opinion is that it's just a lossy IR, if we write
enough helpers to inspect the decl both for AST transform and C++
codegen I'm certain we enable better codegen capabilities."*

The conclusion is right. The reason is too kind to it.

## What it actually is

Not a stage-1 output. `pygen` ASSEMBLES it - `generate.py` builds the
dict, `grpc_schema.annotate()` stamps wire naming into it that only
stage 2 knows, and it is written AFTER the wrappers are emitted. Two
comments in that file say so: *"the wrappers are emitted before the
manifest is assembled."*

What it is instead is a dispatch table, read at run time:

- `server.py` - *"Handlers are generated in a loop from the
  manifest"*; `WireCodec(manifest)`, `FaultCodec(manifest, pool)`
- `remote.py` - `self.manifest["wrappers"].get(cls_name)`, per call
- `wire.py` - encodes and decodes fields by looking types up in it

So the mapping from a Python name to a wire call does not live in
generated code. It lives in JSON, interpreted by a generic loop.

Three symptoms, and they are the ones goal 2 in CLAUDE.md predicts:

1. Every one of those modules is `dict[str, Any]` at the boundary, so
   `--strict` proves nothing about any of them.
2. `check_manifest` exists only because the table can be silently
   wrong. Its own comment: *"A manifest from another generator would
   not fail here - it would answer wrong, one lookup at a time."*
3. Those four modules are hand-written BECAUSE they are interpreters.
   Emit a dispatcher with one method per RPC and most of them stop
   having a reason to exist.

`grpc_schema.pb` is a different thing and STAYS. It is the protobuf
descriptor set, needed for server reflection and for building
messages, and it is not a mapping table.

## The sequencing constraint, which is the part that bites

There are THREE sources of truth today, not two:

1. the declaration IR, through `huggorm_dsl.read`
2. `manifest.json`
3. **reflection on the compiled extension** - `pygen` calls
   `importlib.import_module("huggorm_bindings")` and reads
   `inspect.getdoc` / `inspect.Signature`

`wire.py` says the wire facts come *"out of the manifest, which got
it from a `_wire` / `_wire_fields`"*. Those facts are DISCOVERED by
reflection at build time and then carried to run time in the JSON.
The manifest is the courier.

So the order is forced:

1. **Teach the declaration the wire facts** - `_wire`,
   `_wire_fields`, the identity policy, adoptability - so they are
   declared rather than discovered.
2. **Then pygen stops importing the compiled extension.** cppgen and
   pygen become genuinely one IR, two backends.
3. **Then the manifest can die**, because nothing is left that only
   build-time reflection knew.

Do it in the other order and the reflection is not removed. It is
moved to run time.

## What it does to the structure

`huggorm-gen` is already the right shape for this, and that is why
the two backends were merged rather than split (see the commit).
A package boundary between cppgen and pygen would have made the
reflection seam permanent.

Two things are provisional until this lands:

- `huggorm_gen/payload/` shrinks. `wiretypes.check_manifest` and
  every manifest-reading helper go with the JSON.
- The `huggorm` / `huggorm-generated` boundary moves. However much of
  `server.py`, `remote.py`, `wire.py` and `faults.py` turns out to be
  interpreter becomes emitted instead.

  **"server.py alone is about 500 lines of it" was a guess, and it is
  wrong.** Measured in 065: the four files are 1291 code lines and 12
  sites read the manifest. `server.py` is 441 lines, of which 94
  build handlers from it. The rest is runtime - the handle table, the
  leases, the codec's shape logic - which does the same thing for
  every type and therefore states nothing a declaration could. Those
  two packages do not merge.

## One more mapping, found while renaming

`huggorm/__init__.py` is about thirty hand-written lines of
`from huggorm_bindings import Hash as Hash`, and every one of them
has to track the declarations. That is a hand-maintained mapping of
Python names - the same species as a hand-written C++ mapping, and it
should be emitted for the same reason. Smaller than the manifest and
independent of it.

## The bindings front door, DONE 2026-09-02

`huggorm_bindings/__init__.py` is emitted by `cppgen/pyinit.py`. The
package directory is EMPTY in the checkout - `.gitignore` has no
exception left in it - and `generate.main` makes the directory before
it writes into it.

**Why this half had no blocker and the other does.** `default.nix`
says it, and the sentence was already there: *"huggorm_bindings and
huggorm_generated still come from the store: one is compiled and the
other is generated, so neither exists in the tree."* Nothing puts a
tree copy of `huggorm_bindings` ahead of the store's, so emitting
into it costs nothing. `huggorm/` is the opposite - `nix run test`
exports `PYTHONPATH="$PWD"` precisely so an edit there is testable
without a rebuild - and that is the whole of the remaining decision.

**The derivation, and it reproduced the hand-written list exactly.**
Twenty-five names, no exceptions and no special cases: every class a
declaration binds, every exported free function, every vocabulary's
words, and the module for each is the declaration's own stem - the
same fact that names the `.cpp` beside it.

**One subtraction, and it is forced rather than stylistic.** A free
function a class names with `@produced(by=...)` is dropped.
`open_store` builds a Store, and `nbemit` binds it as
`Store._ctor_from` and as NO module-level function - so
`huggorm_bindings.store` has no `open_store` in it at all. A front
door naming it does not offer a redundant spelling; it fails to
import. The hand-written file omitted the name too and had nothing
that could say why.

That last fact refuted a claim in the first draft of the emitter's
own docstring, which said the name was "still in
`huggorm_bindings.store` for anything that wants it". The
perturbation below is what said otherwise.

### PERTURBED, both halves

Both fail the BUILD rather than a test, and harder than expected -
`huggorm_generated` imports these names from the front door, so a
wrong list stops the generated package from importing at all.

- Drop the producer subtraction: `ImportError: cannot import name
  'open_store' from 'huggorm_bindings.store'`.
- Drop the vocabulary half: `ImportError: cannot import name
  'BuildMode' from 'huggorm_bindings'`.

The stub gate (`__init__.pyi exports ..., the package exports ...`)
is still there behind them, and it is now a real cross-check rather
than a check against a hand-written list: cppgen derives the front
door from the corpus and pygen derives the stub from the manifest.

## What is left

`huggorm/__init__.py`, and the decision above. Everything else in
this task is done.
