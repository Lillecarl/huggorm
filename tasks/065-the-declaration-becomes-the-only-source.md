# The declaration becomes the only source

**OPEN.** The plan for `tasks/064`. Carl: *"ditch the IR in favor of
using the Python DSL decl as the source of truth always, this will
need us to make helpful functions for both analyzing the AST and
importing to get full type information."*

## One correction to 064 first

064 says the first step is to *teach the declaration the wire facts*,
because `wire.py` calls them *"out of the manifest, which got it from
a `_wire` / `_wire_fields`"*.

That is no longer true, and it changes the size of the job. Read
`cppgen/manifest.py:entry()`: it takes a DSL `Class` and derives
`wire`, `wire_fields`, `threading`, the service names, the whole
entry. `pygen/generate.py` then takes that dict WHOLE, through
`declared_entries()`. No class fact is reflected any more.

What actually happens is a round trip:

    decl/pathinfo.py
      -> nbemit writes  cls.attr("_wire_fields") = ...   (a C++ string)
      -> the C++ compiler
      -> import huggorm_bindings
      -> getattr(cls, "_wire_fields")
      -> manifest.json
      -> run time

The declaration states the fact at the top. The bottom reads it back
off a compiled object. Every step between is a courier.

## What pygen still reflects, and where each fact is declared

Six sites, and five of them read something **cppgen itself emitted**:

| site in `pygen` | reads | already declared in |
| --- | --- | --- |
| `_wrapper_classes` | classes with `_threading` | every `NANOBIND` decl |
| `_enum_classes` | `str`+`Enum` subclasses | `decl/words.py` |
| `_free_functions` | module-level callables | `nbemit.public` |
| `extract_enum` | members and doc | `decl/words.py` |
| `extract_errors` | the hierarchy, `_wire_fields` | `decl/errors.py` |
| `getattr(bindings, name)` | a `type` object, for its name | `declared_returned()` |

So the compiled extension answers nothing a declaration does not
already say. It is used to ENUMERATE, and the enumeration is a list
this repo wrote.

Two facts are the exception, and both live in the one hand-written
file in `huggorm-bindings`:

- `_errors_module` - which module holds the exceptions
- `_async_twins` - `{"pathlib.Path": "anyio.Path"}`

They are the whole gap. Not the wire facts.

One more thing found while reading: `_wrapper_classes` excludes a
class with `_async = False`. Nothing emits `_async`. That filter is
dead, left from the deleted mock.

## The plan, in four phases

Each phase leaves the tree green and states the perturbation that
proves it.

### 0. A corpus, not a loop over a list

Five functions in `cppgen/generate.py` open with the same three
lines - `for name in NANOBIND: mod = read(DECLARATIONS / name)`. Each
one then re-derives what it needs. `read()` answers about ONE file,
and cross-file names arrive through a `uses` dict each caller walks
itself.

Add the missing type: a set of declarations, read once, that answers
the questions an emitter asks. `classes`, `unions`, `functions`,
`enums`, `errors`, `returned`, and `by_name` across the whole set.

It belongs in `huggorm-dsl`, beside `read.py`, because "a set of
declarations" is a fact about the language. `huggorm-decl` builds the
instance from its own lists, which is where those lists already are.

This is the "helpful functions" Carl asked for, and it is the phase
that makes the next three small.

**Pure refactor. Nothing about the output changes.**

*Proof:* the emitted tree is byte-identical before and after.

### 1. The last two markers move into declarations

`_errors_module` is derivable: `ERRORS = "errors.py"` already names
the declaration, and `PACKAGE` already names where a module lands.

`_async_twins` needs one new word in the DSL. It is keyed by a
FOREIGN type - `pathlib.Path` - so it is not a decorator on a
declared class. A module-level table in a declaration fits, the way a
union alias already does.

Then `huggorm_bindings/__init__.py` is emitted, and the leaf holds no
hand-written source at all. `huggorm/__init__.py`'s thirty re-export
lines - 064's runner-up - are the same emitter and go with it.

*Proof:* delete a re-export by hand; the next build puts it back.
Remove a class from a declaration; the name leaves both `__init__`
files.

### 2. pygen stops importing the compiled extension

Replace all six sites with corpus queries. Delete the dead `_async`
filter.

The consequence is the point. `huggorm-generated` stops build-
depending on `huggorm-bindings`. The two leaves become siblings
rather than a chain, they build in parallel, and a change to the
Python surface stops waiting on a C++ compiler.

One decision this phase forces. `check_error_contract` BUILDS an
error and reads its parts back, to prove `cls(*parts)` round-trips.
That is a real runtime check, not reflection standing in for a fact.
It moves to the suite, where importing the compiled package is
honest. It does not move to run time.

`smoke_test.py` keeps its imports. It is a GATE - it compares what
was emitted against what compiled - and that is the one job import
reflection is right for.

*Proof, and it is the strong one:* build `huggorm-generated` with
`huggorm_bindings` **not installed**. An `ImportError` cannot be
faked. If it builds, the reflection is gone.

### 3. The manifest dies, because nothing needs a courier

Only now. Emit the dispatch instead of interpreting a table:

- `server.py` - one handler per RPC, not a loop over `manifest`
- `remote.py` - an emitted client, not `manifest["wrappers"].get()`
  per call
- `wire.py` - emitted encode and decode per message
- `faults.py` - an emitted error table
- `payload/wiretypes.check_manifest` and every manifest reader go

`grpc_schema.pb` STAYS. It is the descriptor set, for reflection and
for building messages. It is not a mapping table.

*Proof:* delete `manifest.json` after a build. The suite still
passes.

## Why the order cannot change

Phase 3 emits a dispatcher, so something must READ the declaration to
write it. Today those facts still arrive by reflection. Do 3 before 2
and the reflection is not removed - it is moved to run time, which is
the worse of the two.

Phase 2 needs phase 1, because two facts have no declaration yet.
Phase 1 is easier to get right after phase 0, because the emitter for
an `__init__` wants the whole set at once.

## What is NOT in scope

The `huggorm` / `huggorm-generated` package boundary. 064 notes it
moves once `server.py` is mostly emitted. That is a consequence of
phase 3, and it is a separate decision after the sizes are known.
