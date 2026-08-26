# Task tracker

One file per issue, named `NNN-short-name.md`. When done, append the
`.done` suffix to the filename instead of deleting:

    mv 001-runner-resolve-race.md 001-runner-resolve-race.md.done

Keep files short: problem, evidence, fix sketch. A closed file keeps
its original text and gains a "## Done" section, so the fix stays
readable next to what it fixed.

Findings reference three architectural reviews: 2026-08-23,
2026-08-25, and 2026-08-26 (the Claude Fable review agent, tasks
045-051). Where they disagree, the later one wins.

## Open, roughly by what blocks what

- 030 (attribute sets on the wire) is closed, which unblocks the
  evaluation server: an attrset is what Nix evaluation mostly hands
  back, and Realize now fetches one in a single round trip.
- 031 (recursive handle tracking) is closed. Identity mapping, the
  wire-value boundary, and idempotent grants on every inbound handle.
- 015 (the real-Nix spike) is the other direction, and everything it
  needs is now in place: a settled surface, a lifecycle that does not
  leak, and a build that lints and typechecks what it produces.
- 029 (TypedDicts for the protocol dicts) is a design question, not a
  defect - and probably wants a stage-by-stage refactor rather than an
  annotation change.
- 026 (typed proxy parameters) is a design question, not a defect. It
  is the one place the two locations genuinely disagree.
- 015 (real-Nix spike) substitutes into the surface 017 settled.
- 008 (transitive policy), 012 (test blind spots), 022 (proto field
  stability), and the derivation half of 025 are hardening. 022 grew
  twice: free-function requests number their fields positionally too,
  and the manifest's "schema": 1 is written by the generator and read
  by nobody.
- 035 (anyio, not asyncio) is half done: the suites are anyio, the
  library is not. The movable half is the runtime and the ping loop;
  the rest waits on 014, because grpclib is an asyncio library.
- 015 (real Nix) is under way: nix::StorePath is bound, validated by
  libstore, and crossing the wire. The next questions are typed errors
  (done: typed, plain, and the colour kept as a field) and which type
  comes next.
- 036 (errors over the wire) is done. A nix error keeps its class and
  its colour across the wire: the hierarchy is declared in the
  bindings and reflected into the manifest, and a failure travels as
  typed messages in grpc-status-details-bin rather than as JSON in the
  status text. Building function values into
  the mock was the point where mock fidelity stopped paying: it was
  reimplementing libexpr to prove things libexpr already does.
- 038 (string enums) is done. ContentAddressMethod and HashAlgorithm
  are StrEnums in the bindings, found by reflection with no
  declaration of their own, and the wire did not move: a member is a
  str. A value read off the wire comes back typed, and libstore stays
  the authority on what the words mean.
- 044 (the store as a graph) is done. references had one edge, one
  way, one path at a time; query_referrers is its inverse and
  compute_fs_closure is the transitive reading that makes either worth
  having. query_valid_derivers and query_valid_paths come with them.
  The enabling change is that a StorePathSet now crosses as base
  NAMES: it retired twenty lines of manual pointer ownership in Cython
  that would otherwise have been copied five times.
- 043 (an optional return) is done. `T | None` is a return type the
  surface can spell, and it needs no new wire machinery: a protobuf
  message field has presence, so an unset one IS the None. A scalar,
  an enum, a container and a two-armed union are each refused with
  their own reason - and a WRAPPED T is refused for every surface at
  once, because every layer adopts a returned proxy into a runner and
  none of them adopts nothing.
- 042 (which store path holds this file) is done. to_store_path
  answers the question parse_store_path cannot: an interpreter lives
  at `<store path>/bin/python3`, which is a file in a store object and
  is not one. The answer is a PAIR, carried by StoreLocation, because
  the store path plus the sub-path is what reaches the file again -
  through real_path, which closes the loop the other way. Two more
  calls follow symlinks first: follow_links_to_store_path for the
  object, follow_links_to_store for the file. The second returns a
  str because its answer is in the STORE's terms rather than this
  machine's, which is also what gives it an rpc.
- 041 (containers inside a wire value) is done, both directions. A
  wire value's field now goes through the same encode and decode an
  rpc field goes through, rather than through a second dispatch that
  had drifted - which is what let PathInfo carry `references` and
  `sigs`. A container parameter may default to None, because a
  repeated field has no presence PROBLEM: absent and empty are the
  same field. `[]` is refused, as a mutable default the four surfaces
  would each carry.
- 040 (store paths as filesystem paths) is half done. Store.real_path
  answers with the REAL directory and raises Unsupported for a store
  with no filesystem. It is a local method and not a remote call, and
  making that expressible was the actual work: a METHOD can now say
  the wire cannot carry it, the way a free function always could. What
  stays refused is a pathlib surface on StorePath itself - a StorePath
  is a name, not a location.
- 039 (parameter defaults) is done. A default is a fact about the
  signature: every generated surface writes the same one, so the wire
  never has to say "absent" and needs no field presence. What may be
  written is checked - an enum member, or a literal that reads back as
  itself - and everything else stops the build. Constructor defaults
  still go the other way, through the overload-derived `optional`. The
  blanket refusal of a None default lifted for containers in 041.
- 037 (tests outside the sandbox) has its mechanism: a `live` marker
  naming what a test needs, hermetic by default so a forgotten mark
  fails loudly in the build, and `nix run --file . test` for the whole
  suite outside it. What is left is what a live test may ASSUME - a
  daemon, or a writable chroot store - and that is what addToStore
  will answer.
- 034 (functions as values) waits on 015. Its analysis is about the
  Python surface, not the mock, so it survives intact - and against
  libexpr the formals are real.
- 032 (log callbacks) and 033 (primops in Python) are the two places
  the flow reverses: C++ calling into Python, on Nix's schedule and
  Nix's thread. Neither can be generated from a binding declaration,
  and 033 is the harder one - a primop runs inside evaluation, so it
  cannot hop threads, cannot await, and its arguments do not outlive
  the call.
- 045 (wire names) and 048 (proto3 optional) want doing BEFORE 022:
  both change the schema, and the lockfile should pin the fixed
  names and the synthetic oneofs, not the current ones.
- 047 (enums across the layers) and 049 (ping resurrection) are
  latent defects with small fixes: the codec crashes on a container
  of enums the schema accepts, and a swept client learns of its
  death from an unrelated error.
- 046 (dunders on value types), 050 (shim signatures stated twice)
  and 051 (the front door) are the DX and maintainability half of
  the 2026-08-26 review. 050 is the one that grows with every bound
  method; 046 and 051 are cheapest before there are users.
- 014 (transport shims) and 016 (evaluation server) are the
  destinations. 016's lifecycle contract is settled and executable -
  a detached evaluator survives its creator's death and a successor
  claims it warm - so what is left of it is the part that needs a real
  evaluator: warm caches, the file graph, background evaluation.

Every build lints and typechecks the code its package owns, and
`nix run --file . check` does the whole tree in about a second (013).

## What the codegen emits

Per wrapped class, three forms plus the wire:

    Async<X>     in-process wrapper, owns the thread hop      (async_x.py)
    <X>Like      the protocol both implementations satisfy    (protocols.py)
    RPC<X>       client class over a handle                   (rpc.py)
    <X>Service   gRPC service, and <X>Msg for a wire-value    (grpc_schema.pb)

Plus one stub package describing the BINDINGS, so the types all of the
above name are not Any to a typechecker (cythonix_bindings-stubs/, 027).
That one is built from the unfiltered surface: the policy drops and the
hierarchy split are rules about the wrappers, not about the bindings.

A class that needs no wrapper gets none of the first three and keeps
its message: it crosses as itself. The smoke test holds the three
Python surfaces to each other by signature, not by isinstance.

## What the bindings now declare

Each marker moved down the stack because a layer above was carrying
the same knowledge by hand. The generator reads all of them:

    _threading   pool | affine            execution policy
    _wire        value | proxy            does it serialize
    _wire_fields message shape + helpers  HOW it serializes  (023)
    _binds       the pxd class it wraps   pxd <-> pyx link   (020)
    _async       False to exclude         generation opt-out

    _abstract    True for a generated base       inheritance   (018)
    _blocking    False if no method can wait     wrap or not   (025)
    _produced    True if __init__ raises         constructible (041)

The PACKAGE declares two more, in its __init__:

    _errors_module   where the exception hierarchy lives      (036)
    _async_twins     a type's async spelling, if it has one   (040)

`_async_twins` maps pathlib.Path to anyio.Path: the binding returns
the sync type and the wrapper hands back the other, which is the one
place the two surfaces should differ. It never reaches the wire.

A class declares itself in its BODY, and that is forced rather than
chosen. Cython refuses any decorator on a cdef class but
`functools.total_ordering` and `dataclasses.dataclass` - "Cdef
functions/classes cannot take arbitrary decorators" - and setting the
attribute afterwards fails too, because an extension type is
immutable.

Module-level functions declare the same two things with DECORATORS,
which a `def` can take:

    @threading("pool")            execution policy
    @binds("describe_store")      the pxd name, when it differs

They set the same attributes, so the generator reads one thing either
way. They return the function itself and never a wrapper: the
generator reads the signature off what the module exports, and a
wrapper would turn every parameter into Any - which the unresolved-type
gate catches.

Every public function is in the manifest either way; the policy
decides only whether it gets an async form and an rpc, and "pool" is
the only legal one - no instance, so no thread to be affine to (021).
An undecorated function says exactly that by carrying no decorator.

Constructor signatures come from the pxd, which is the only place they
exist at all - Cython exposes no signature for __cinit__ (019).
