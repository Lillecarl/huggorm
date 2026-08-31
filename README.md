# huggorm

Nix, bound to Python through nanobind, with the bindings THEMSELVES
generated from a declaration, and everything above them generated from
the same declaration.

One file decides six surfaces: the C++ binding, the type stub, the
manifest entry, the async wrapper, the RPC client and the gRPC schema.
Adding a type means editing one declaration and nothing else. That is
the whole premise, and it is what a reviewer should push on.

A declaration is read twice: imported, so Python resolves any
`NIX_VERSION` branch, and parsed, for the C++ in its bodies and the
order of its methods. No body ever runs - `def` defines, it does not
call - so a declaration can name a C++ type this machine has never
compiled, and no surface above the bindings waits on a compiler.

This file is how to DRIVE the repo. `docs/quickstart.md` is how to USE
the library. `tasks/README.md` is what the design is and why, and each
`tasks/NNN-*.md` is one decision with the alternatives it rejected.

## Commands

Everything runs through `nix`, from the repo root. No devshell needed,
though `nix develop --file . shell` gives one.

    nix build --file . huggorm --no-link     # THE gate: builds all four
                                              # layers, lints, typechecks,
                                              # runs the hermetic suite
    nix run  --file . check                   # lint + typecheck the whole
                                              # tree, ~1s, no rebuild
    nix run  --file . test                    # the WHOLE suite, live tests
                                              # included, outside the sandbox
    nix run  --file . test -- -m live         # only the tests that need a
                                              # real store
    nix run  --file . test -- -k closure -x   # any pytest arguments

The build gate is the one that matters. It runs `pytest -m "not live"`
inside the sandbox, so a test that touches the machine's real store
must carry `@pytest.mark.live` or it fails there loudly.

`nix run --file . test` puts the working tree's `huggorm/` ahead of
the installed copy, so an edit to the hand-written layer is testable
without a rebuild. `huggorm_bindings` and `huggorm_generated` always
come from the store: one is compiled and the other is generated, so
neither exists in the tree.

### Reading what the build produced

    nix run --file . show                # where everything landed
    nix run --file . show -- manifest    # the contract between stages
    nix run --file . show -- proto       # the whole wire schema, as text
    nix run --file . show -- surface     # every emitted Python module

`proto` is the one a reviewer wants. The schema is a binary
FileDescriptorSet that no editor renders, and it is generated - so the
only honest way to review the wire is to read what actually came out.

The emitted C++ is not in the tree either. It is written into the
build's copy of `huggorm-bindings`, so to read it:

    nix build --no-link --print-out-paths --file . bindings-src

### Reading a dependency's source

    nix build --no-link --print-out-paths --file . pkgs.nix.src

Nix's own headers are in `pkgs.nix.dev`. The repo's rule is to anchor
on upstream source rather than guess, and several declarations carry a
comment about an upstream behaviour that was read rather than assumed.

## Layout

    packages/
      huggorm-dsl/        the LANGUAGE: what a declaration may say
      huggorm-decl/       the DECLARATIONS, and the C++ helpers they name
      huggorm-gen/        the EMITTERS: two backends over one reader
      huggorm-bindings/   LEAF: the nanobind extensions
      huggorm-generated/  LEAF: the emitted API surface
      huggorm/            the hand-written layer: server, client, codec
    examples/             runnable demos - not shipped in the package
    docs/                 user-facing; quickstart.md is the front door
    tasks/                one file per decision, NNN-name.md[.done]

Three things are easy to confuse and are kept apart. The LANGUAGE is
`declare.py`. A DECLARATION is a document written in it. An EMITTER
turns one into an output. Neither leaf holds hand-written source:
each is setuptools running a generator over what the other three say.

### huggorm-dsl

    src/huggorm_dsl/
      declare.py      the vocabulary a declaration is written in
      read.py         ast.parse -> Module, Class, Method

No declaration and no emitter, which is what lets huggorm-decl and
huggorm-gen depend on it without depending on each other. `read.py`
is here rather than with the emitters because parsing is a fact about
the language: two backends parse the same way or they are not reading
the same language.

### huggorm-decl

    src/huggorm_decl/
      __init__.py     which declarations exist, and which own a module
      decl/<name>.py  one Nix class, named after its header
      decl/README.md  why they sit in their own directory
      cpp/<name>.hpp  C++ this repo writes, for what a declaration CALLS
      cpp/README.md   the rule for what belongs in there

The two things a person maintains: the declarations, and the helpers
they name. `include_dir()` is how the bindings build compiles against
`cpp/`, the way nanobind exposes its own headers.

`__init__.py` names the declarations that own a module. That list is
here rather than with an emitter because which declarations exist is
a fact about this set of documents, not about a backend reading them.

### huggorm-gen

    src/huggorm_gen/
      cppgen/         -> huggorm_bindings: C++, stubs, enums, errors
      pygen/          -> huggorm_generated: async, protocols, RPC, proto
      payload/        -> neither: hand-written Python that SHIPS
    gates/nbcheck.py  emitted C++ against hand-written nanobind

One package, not two, because both backends read one IR from one
reader. pygen still reflects on the compiled extension for enums,
errors and free functions; that seam is being closed, and a package
boundary would have made it permanent. protobuf is pygen's extra
rather than a dependency, so compiling the bindings does not drag it
in.

One Nix header, one declaration, named after it:
`nix/store/store-api.hh` is `decl/store.py`, and
`nix/store/path-info.hh` is `decl/pathinfo.py`. The exception is
`decl/words.py`, because a vocabulary has no C++ and no extension to
live in - see below.

`decl/store.py` is the biggest and the most current - read it first,
for what a proxy looks like. `decl/pathinfo.py` is the wire-value to
read: it binds `nix::ValidPathInfo` and carries the `_from_parts` that
rebuilds one. `decl/hash.py` is the shortest one that shows the whole
idea - two facts on the wire, three renderings marked `@local` that
never leave this side. `decl/path.py` is the smallest complete one,
and the place to start if `store.py` is too much at once.

`decl/words.py` is the odd one out and is not a binding at all: the
StrEnums whose members ARE the strings a Nix parser takes. Nothing
about them compiles.

A declaration not in that list emits nothing: `decl/nixstore.py` and
`decl/storefns.py` are read only by `gates/nbcheck.py`, which compares
them against the hand-written nanobind in `~/Code/nanopynix` - a
corpus to beat rather than a reference to match - and is skipped on a
machine without it.

### huggorm-bindings

    huggorm_bindings/
      __init__.py     what the package exports, and in which order
    setup.py          runs cppgen, then one Extension per declared module

`__init__.py` is the only hand-written file. `setup.py` runs the
emitter at import - before setuptools is told the sources exist - and
compiles what it wrote: one `.cpp` per declared module, plus
`errors.py` and the enum modules. The C++ helpers a declaration NAMES
are not here either; they are `huggorm_decl/cpp/`, with the
declarations that name them.

`setup.py`'s `LIBRARY` dict is the one place that says which library
each module links. A declaration names the C++ it binds; which package
ships that C++ is the build's fact.

### huggorm-generated

    generator/src/codegen/
      model.py        declaration entries into protocol dicts
      surface.py      names for the PYTHON surface, and what a protocol
                      may carry
      grpc_schema.py  names for the WIRE, and the FileDescriptorSet
      wiretypes.py    how a declared type STRING is spelled
      emitter.py      protocol dicts -> Python, via ast.unparse
      runtime.py      copied into the package as _runtime.py
      generate.py     the driver, and every contract check it runs
      smoke_test.py   the gates that hold the surfaces to each other

Read `wiretypes.py` first: it is small and it is where a type's
spelling is decided. Then `model.py`'s `check_*` functions - each one
is a rule the build refuses to break, and each names the failure it
prevents.

Nothing here reflects a compiled class any more. Every shape comes
from `huggorm_gen.cppgen.generate`: `declared_entries`, `declared_functions`
and `declared_returned`. The compiled package is still imported, and
for one thing only - to enumerate which classes to generate for.

### huggorm

    huggorm/
      wire.py       the codec both sides share. Knows shapes, no types.
      server.py     manifest -> gRPC service, dispatch, handles
      remote.py     the client
      lifecycle.py  connections, leases, detach and reclaim
      faults.py     a typed error, across the wire
      tests/        the suite, hermetic unless marked live

`wire.py` is the sharpest single file: it names no concrete type at
all. Every type it acts on comes out of the manifest.

## How a change flows

Adding a store call is the common case, and it touches two files:

1. `packages/huggorm-decl/src/huggorm_decl/decl/store.py` - the method, with a
   Python-style annotation and a docstring.
2. a test in `huggorm/tests/test_store.py`, and one in
   `test_remote.py` if it crosses the wire.

`huggorm_decl/cpp/store.hpp` is the third file, and only when the declaration
cannot say it: something a whole module needs, such as a startup hook
or an exception translator. A DECISION - how to render a hash, which
of two constructors a value takes - goes in the declaration itself, as
a `Cxx(...)` in the method's body. Every build prints how many of each
class's methods were derived and how many carry a body.

Nothing else is edited. The emitters write the binding, the stub and
the manifest entry, and the four surfaces follow from the manifest. If
the codegen cannot express something, that is the finding - and the
fix belongs in the declaration or the emitter, not in a hand-written
wrapper.

## What the build refuses

Worth knowing before reading the generator, because most of it exists
to make one of these fail early:

- a parameter whose type cannot be resolved (it would be `Any`)
- a wire-value with no `_wire_fields`, no round-trip helpers, or a
  proxy inside one
- a hand-written `_from_parts` that never names one of its own wire
  fields, and - in the suite - a wire value whose round trip is not
  exercised with each part holding two different values
- a default the surface cannot WRITE, or a mutable one
- a method returning a container of wrapped types, or an optional one
- a base and a subclass whose shared method signatures differ
- an emitted module that imports something it does not use
- the three Python surfaces disagreeing on any signature

Each has a `tasks/` file naming the bug it prevents.

The C++ compiler is a gate too, and it is the one the emitter leans
on. An emitter that spells a type wrong does not write a bad binding
that imports; it fails to compile.

## Conventions

- **Tests are perturbation-verified.** A gate that has not been seen
  to FAIL is not known to hold. The habit is: break the thing on
  purpose, watch the named test fail, put it back. Most `tasks/` files
  record which perturbation was run.
- **Hermetic by default.** A test that needs the machine's real store
  carries `@pytest.mark.live`. The build sandbox has no daemon and no
  writable store, so an unmarked one fails there.
- **Prose follows ASD-STE100.** Short sentences, active voice, one
  idea each. Comments say WHY, and name the alternative that was
  rejected.
- **One concern per commit.** If the subject needs "and", it is two.

## The mock is gone

`fake-library/` was a C++ stand-in this repo grew before real Nix was
linked. It is deleted (tasks/060). Every binding here names libstore
or libexpr.

libstore is `Store` (`nix::Store`), `StorePath`, `PathInfo`
(`nix::ValidPathInfo`), `StoreLocation`, `Hash`, `Signature`,
`ContentAddress`, `DrvOutput`, `Realisation`, `MissingPaths` and the
`DerivedPath` union. libexpr is `EvalState` and `Value`.

The one piece of C++ this repo writes for itself is
`huggorm_decl/cpp/eval.hpp`. A `nix::Value` lives in the collector's heap and
Python's heap is not scanned, so a wrapper needs a ROOT; and a value
is not self-describing, because an attribute name is a `Symbol` only
the producing state can render. `huggorm::Bridge` holds both. The
census prints its line count on every build, so the number a
declaration could not derive is one nobody has to go looking for.

## Where to start reading

For the DESIGN: `tasks/README.md`, then the newest `tasks/` files -
they are the current thinking, and the older ones record how it got
there.

For the CODE: `packages/huggorm-decl/src/huggorm_decl/decl/path.py`, then
`nbemit.py` beside it, then `huggorm/huggorm/wire.py`.

For the OUTPUT: `nix run --file . show -- proto`.
