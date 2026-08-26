# cythonix

Nix, bound to Python through Cython, with everything above the
bindings GENERATED from the bindings.

One declaration next to a binding decides four surfaces: the async
wrapper, the protocol, the RPC client and the gRPC schema. Adding a
type means editing a `.pyx` and nothing else. That is the whole
premise, and it is what a reviewer should push on.

This file is how to DRIVE the repo. `docs/quickstart.md` is how to USE
the library. `tasks/README.md` is what the design is and why, and each
`tasks/NNN-*.md` is one decision with the alternatives it rejected.

## Commands

Everything runs through `nix`, from the repo root. No devshell needed,
though `nix develop --file . shell` gives one.

    nix build --file . cythonix --no-link     # THE gate: builds all four
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

`nix run --file . test` puts the working tree's `cythonix/` ahead of
the installed copy, so an edit to the hand-written layer is testable
without a rebuild. `cythonix_bindings` and `cythonix_generated` always
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

### Reading a dependency's source

    nix build --no-link --print-out-paths --file . pkgs.nix.src

Nix's own headers are in `pkgs.nix.dev`. The repo's rule is to anchor
on upstream source rather than guess, and several bindings carry a
comment about an upstream behaviour that was read rather than assumed.

## Layout

    fake-library/          a C++ stand-in, on its way out (see below)
    cythonix-bindings/     the Cython bindings - the bottom of the stack
    cythonix-generated/    the generator, and the package it emits
    cythonix/              the hand-written layer: server, client, codec
    examples/              runnable demos - not shipped in the package
    docs/                  user-facing; quickstart.md is the front door
    tasks/                 one file per decision, NNN-name.md[.done]

### cythonix-bindings

    cythonix_bindings/
      c_<name>.pxd    what C++ declares. Nothing of ours.
      <name>.pxd      what our cdef classes declare, for sibling modules
      <name>.pyx      the binding
      _cpp/<name>.hpp C++ this repo writes, for what a pxd cannot SAY
      _cpp/README.md  the rule for what belongs in there
      errors.py       the exception hierarchy, mirroring libnixutil's
      _declare.py     the decorators a free function carries

One Nix header, one binding module, named after it:
`nix/store/store-api.hh` is `store.pyx`.

`store.pyx` is the biggest and the most current - read it first. Its
`PathInfo` and `StoreLocation` show what a declared wire-value looks
like; `Store` shows a proxy.

### cythonix-generated

    generator/src/codegen/
      pxd.py          parse the pxd - the C++ surface as data
      model.py        reflect the live bindings into protocol dicts
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

### cythonix

    cythonix/
      wire.py       the codec both sides share. Knows shapes, no types.
      server.py     manifest -> gRPC service, dispatch, handles
      remote.py     the client
      lifecycle.py  connections, leases, detach and reclaim
      faults.py     a typed error, across the wire
      tests/        the suite, hermetic unless marked live

`wire.py` is the sharpest single file: it names no concrete type at
all. Every type it acts on comes out of the manifest.

## How a change flows

Adding a store call is the common case, and it touches four files in
one direction:

1. `_cpp/store.hpp` - only if a pxd cannot say it. A type with no
   default constructor, or a member reached through `Store::config`.
2. `c_store.pxd` - the C++ declaration.
3. `store.pyx` - the binding, with a Python-style annotation.
4. a test in `cythonix/tests/test_store.py`, and one in
   `test_remote.py` if it crosses the wire.

Nothing above that is edited. The generator reads the pxd and the
compiled module, and the four surfaces follow. If the codegen cannot
express something, that is the finding - and the fix belongs in the
generator, not in a hand-written wrapper.

## What the build refuses

Worth knowing before reading the generator, because most of it exists
to make one of these fail early:

- a parameter whose type cannot be resolved (it would be `Any`)
- a wire-value with no `_wire_fields`, no round-trip helpers, or a
  proxy inside one
- a default the surface cannot WRITE, or a mutable one
- a method returning a container of wrapped types, or an optional one
- a base and a subclass whose shared method signatures differ
- an emitted module that imports something it does not use
- the three Python surfaces disagreeing on any signature

Each has a `tasks/` file naming the bug it prevents.

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

## The mock

`fake-library/` and the `Mock*` bindings are a C++ stand-in this repo
grew before real Nix was linked. It is on its way out. Each mock class
took a `Mock` prefix the moment its real counterpart landed, so the
prefix is a map of what is left to do.

Real Nix is `Store`, `StorePath`, `PathInfo`, `StoreLocation`. The
`Mock*` classes still earn their place as the only exercise for
shapes real Nix has not reached yet - a class hierarchy, a value tree,
an affine-threaded object.

## Where to start reading

For the DESIGN: `tasks/README.md`, then the newest `tasks/` files -
they are the current thinking, and the older ones record how it got
there.

For the CODE: `cythonix-bindings/cythonix_bindings/store.pyx`, then
`cythonix/cythonix/wire.py`, then
`cythonix-generated/generator/src/codegen/wiretypes.py`.

For the OUTPUT: `nix run --file . show -- proto`.
