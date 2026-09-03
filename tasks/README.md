# Task tracker

WHAT this is all for is in `CLAUDE.md`, under "What this is for": an
evaluation service that outlives its callers, and the milestone that
says it is real. This file is the board - what is open, what is done,
and the design decisions behind each. It says how, and the purpose
says why.

One file per issue, named `NNN-short-name.md`. When done, append the
`.done` suffix to the filename instead of deleting:

    mv 001-runner-resolve-race.md 001-runner-resolve-race.md.done

The first line after the title OPENS with the status in bold, in one
of five words: **OPEN**, **MOSTLY DONE**, **PARKED**, **DONE**,
**CLOSED**. A clause may follow it - "CLOSED, by the question going
away" says more than the word alone - but the word comes first, so a
grep finds it. The word and the filename suffix say the same thing,
and checking one against the other is how the tracker is audited: 063
reached DONE in its text while its filename still said open.

Files numbered 051 and below predate the convention and carry no
status word. The audit in CLAUDE.md reports each of them as
``line=none``, which is the audit saying it cannot confirm rather
than the two answers disagreeing. Every file from 052 up has one.

Keep files short: problem, evidence, fix sketch. A closed file keeps
its original text and gains a "## Done" section, so the fix stays
readable next to what it fixed.

Update the file as the work happens, not at the end. A task that
records only its conclusion loses the measurement that produced it,
and the refutations - a gate that turned out not to hold, a rationale
that turned out to be false - are the part nothing else in the repo
records.

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
All seven findings of the 2026-08-26 review are resolved except 050,
which was superseded rather than fixed.

- 051 (the front door) is done. `import huggorm` re-exports the real
  surface by kind, the demos moved to examples/, the server reports
  through a logger, and the pool takes a size. A test derives the
  export list from the two packages rather than keeping a second copy.
- 056 (PathInfo is declared twice) is done. One declaration, and the
  one the spike gate measures is the one we ship.
- 055 (adjacent Nix versions) is OPEN and undecided on purpose. The
  C++ answer - preprocessor ladders - does not transfer, because four
  generated surfaces sit above the bindings and none of them has a
  preprocessor. Resolving the version at GENERATE time is the
  recommendation; the sharp open question is whether the wire must
  stay stable across a version delta.
- The source-format question is DECIDED. A Python declaration, read
  with `ast.parse` and never run, is the source; `huggorm-idl` is
  the package. Cython is gone with the question: 053 is the spike
  report that argued the direction, 054 is why pure mode lost, and
  050 records what was learned on the way.
- 059 (sum types on the wire) is DONE. DerivedPath and its parts
  cross as a real tagged union rather than by shape, which is better
  than either encoding upstream uses. The last piece was how a union
  spells a C++ arm that WRAPS a declared one: `Variant(...)` on the
  alias says it, and the emitter writes the visit both ways.
- 060 (the mock goes) is done. `fake-library/` is deleted and no
  binding names a mock.
- 061 (one table for the markers) is done. The table drives
  validation, the `@abstract` split landed - a declaration states the
  C++ FACT and `Class.constructs` answers "is there a door", so
  nix::Store says it is abstract AND keeps its factory - and
  diagnostics carry `path:line:col` and are collected. The reader has
  one cross-class check, which is why collection produces no bogus
  errors.
- 062 (the suite fills the disk) is OPEN.
- 063 (the hpp files hold mappings) is DONE. Its three original
  fronts closed, `errors.hpp` stopped naming its Python module,
  `open_store` is emitted, and `derived_path.hpp` is deleted - the
  union's alias carries a `Variant(...)` and the emitter writes
  `as_arms`, `from_arms` and `held` from it. Multiple inheritance was
  the shape Carl asked about and the file records why it is not:
  inheritance says "is a", which is the reverse of a sum type. cpp/
  is 290 code lines in three files, all helpers.
- 067 (a caster instead of a named conversion) is DONE. A generated
  nanobind type_caster casts `nix::DerivedPath` to the arms Python
  has, so no declaration body converts and a signature says what
  libstore says. `huggorm::held` went with it. The Python surface did
  not move: the stubs still read `-> DerivedPath`. Spiked in a jj
  workspace first (068), which is why the main tree never carried a
  version that did not work.
- 069 (nothing can be built yet) is MOSTLY DONE. `Store.build_paths`
  is bound, so the DerivedPath union has the consumer it was built
  for - one `Cxx` line, because the caster from 067 already made the
  parameter a `std::vector<nix::DerivedPath>`. Two hermetic tests: a
  held path builds as a no-op, an absent one raises in libstore's own
  words. Four more Store methods came with it and needed no new DSL
  at all - `ensure_path`, `add_temp_root`,
  `query_substitutable_paths`, `topo_sort_paths` - so a caller can
  now build a thing, keep it from the collector, ask what could be
  fetched instead, and walk a closure in reference order. What is
  left needs a C++ ENUM on the surface: the build MODE, and
  `buildPathsWithResults` through the `BuildResult` it would have to
  declare. The marker for that is 070, and the "ONE ready user" this
  file used to give as the reason to wait was WRONG - two shipped
  vocabularies already had a C++ enum behind them.
- 070 (a vocabulary checked against its enum) is MOSTLY DONE.
  `@words` takes `enumerated=Enumerated(...)`, and the emitter writes
  the direction `parsed_by` never had: a word coming BACK from
  libstore, as a switch with no `default:` under `-Werror=switch`.
  Both shipped vocabularies use it and both lost a hand-written
  mapping. Four perturbations, all in the real build: a word removed
  fails the compile, the same break BUILDS without the flag, a
  misspelling compiles and fails a test, and a wrong enumerator name
  is refused by name. BuildMode is bound on `Store.build_paths` and
  is the first vocabulary whose words are OURS: nix::BuildMode has no
  parser and no rendering, so the emitter writes both directions and
  emits the read-back switch even though nothing returns one - the
  switch IS the gate. BuildResult's two status enums came off the
  queue in 071 and TrustedFlag on 2026-09-02, with
  `Store.is_trusted_client` as its user: three answers rather than
  two, because a store can also say nothing. Its enum is unscoped and
  over `bool`, and the switch gate was shown to hold for that shape
  rather than assumed to.

  GCAction landed with 074 and has its switch gate. What is left is
  FileIngestionMethod alone, and it has no binding that takes one at
  all - so it would be a vocabulary with no user and no gate.
- 080 (a parameter cannot say its width) is OPEN. `params[].type` is
  one string read as a Python annotation by the stubs and as a wire
  type by the schema, so a proxy method may not take a `uint64_t` -
  the build refuses one by name. A second key beside `type` is the
  obvious fix and `model.py` cannot reflect one, so `check.py` would
  diff against a shape reflection cannot produce. Decide with 022.
- 079 (every int crosses the wire signed) is DONE. The wire has two
  integers: `uint` is a uint64 and `int` is a sint64, from the C++
  spelling the alias already carried. Six fields moved and three did
  not - the file's own claim that every int field would change was
  wrong, and so was its plan: the "cheap half" it named, a build-time
  refusal of an out-of-range u64, is unenforceable because a range is
  a run-time fact.
- 082 (a failed import reads as a working file) is DONE. `load`
  refused nothing and answered None, and the reader fell back to the
  tree - which keeps the same nodes for every declaration here, since
  none has a `NIX_VERSION` branch. So a file that did not import read
  as one that did, and so did every file importing from it. It
  refuses now, with Python's own reason. Second half: `@staticmethod`
  and `@classmethod` are refused too - reachable since 075, and
  measured dropping the first parameter of every method that wrote
  one.
- 083 (nothing decides when to forget) is OPEN, and has its watcher.
  `huggorm.Watcher` is written against `EvalStateLike`, so one object
  serves the in-process and the remote surface. It found 016 wrong on
  the way: a closure is NOT the `cached_files` diff around one
  `eval_file`. The diff says what an evaluation newly CACHED rather
  than what it READ, so the second root importing a shared file gets a
  diff that omits it and answers stale - measured, and gated by the
  test that reverting to the diff model is the only thing that fails.
  It records a full SNAPSHOT per root instead, which is a superset of
  the closure and cannot miss, and it over-forgets by an amount the
  file measures. Bookkeeping is separate from noticing: `changed()` is
  told, `rescan()` stats, and both call one step - so every gate runs
  with no sleeps. The inotify adapter is decided
  (`asyncinotify`, Linux only) and DEFERRED by Carl behind 033 and
  032: the spec, the codegen and the binding details come first.
  `rescan()` stays the change source until then.
- 081 (an input that reached no output) is DONE. The general form of
  073, 075 and 078: an emitter skips what it does not recognise, and
  a skip reads as an absence. The fear it was opened with was wrong -
  every legitimate skip is already something the declaration SAYS, so
  no new word was needed. Two censuses, at the two seams that produced
  the three bugs. `census_read` compares the RAW parse against where
  the reader put each definition, because everything else is built
  from the read and would agree with it; it catches 075 exactly, and
  errors.py at class grain, where a method turned out not to be
  droppable at all because `_resolve` appends a ClassDef whole.
  `census_written` compares what the reader kept against the emitted
  TEXT, inside `emit_module` where both exist. Each was seen to fail
  on the bug it is for. The surfaces above the bindings are emitted
  from the MANIFEST and are a different question, unasked because no
  bug has asked it.
- 016 (evaluation server) is OPEN, and has its first warm cache.
  `eval_file` is declared, so libexpr's `fileEvalCache` is reachable:
  evaluate a file, delete it, evaluate it again, and the answer comes
  from the cache. A fresh state asked the same thing in the same
  moment says "opening file ... No such file or directory", which is
  the control. The MILESTONE is reached: a successor claims the state
  after a sweep and answers for a file deleted before it, while a
  fresh state on the same server cannot. Watched files have their
  first half: libexpr will not say which files it read - the caches
  are private and `rootFS` cannot be substituted - so
  `EvalState.cached_files()` reaches the cache from our own header,
  by the explicit-instantiation rule, with nixpkgs untouched. It sees
  a file reached by `import`, which our own boundary never could.
  Per-path invalidation is built too: `EvalState.forget_file()` drops
  one file's warm evaluation and keeps the rest, where the only public
  way - `resetFileCache()` - also clears the fetched flake inputs. It
  erases BOTH spellings, because the eval cache is keyed by the
  resolved path. Forget the CLOSURE, not the file: the cache holds no
  edge from an importer to its import, so forgetting the changed file
  alone leaves the importer silently stale - measured, gated, and kept
  as a negative control. What is left is a watcher that decides WHEN,
  and background eager evaluation.
- 078 (a declaration nobody lists reaches nothing) is DONE. `corpus()`
  censuses `decl/` against the lists and the build fails when they
  disagree. The plan in the file was wrong: "every `*.py` in exactly
  one of the three lists" fails on nixstore.py and storefns.py, which
  are unlisted on purpose and read only by `gates/nbcheck.py`. They
  have a fourth list now, so the census can be exhaustive.
- 074 (garbage collection needs an input record) is DONE. A store
  collects its own garbage. The input record needed no new machinery -
  a constructible wire value already was one - and the only thing that
  refused was rebuilding a set member, which cost one keyword on
  `@reads` and deleted a hand-written `_from_parts` elsewhere.
- 075 (an accessor reads a table the emitter can derive) is DONE.
  `_accessor` and its two-row `CXX_OPTIONAL` are gone. The reading was
  right about the table and wrong about how it was reached: the
  function was unreached, and the perturbation that showed it found a
  `@property` accessor being dropped from every emitted output in
  silence. Read the file for that half.
- 077 (sixteen tasks answer neither signal) is DONE. Every task file
  carries a status word now and the check reports no mismatch. The
  reading moved two: 015 (real-Nix spike) to DONE, 050 (shim methods)
  to CLOSED on a premise Cython took with it. 034 looked closed and is
  not - what Carl aborted was the MOCK version of it.
- 076 (an accessor that is an attribute) is OPEN. `@property` in a
  declaration is refused, and the file says what honouring it would
  cost - four emitters reading one predicate. Do it when a declaration
  needs an attribute, not for prettiness.

  The wire question is settled: a word crosses as a STRING, with a
  generated mapping at both ends, because a word is easier to read
  off a wire than a number and these calls are far too expensive for
  the bytes to matter. Protobuf's own enums would have worked; the
  argument written against them was aimed at C++'s numbering rather
  than at a proto's, and it fell.
- 071 (a build result is a sum with an exception in it) is DONE.
  `Store.build_paths_with_results` is bound and
  answers a `KeyedBuildResult` per target: two arms, `success` and
  `error`, exactly one present, and it never raises. Carl decided
  that - a BuildResult does not raise in Nix - so `error` ANSWERS
  with the typed BuildError rather than throwing it.

  Two vocabularies rather than one merged list of sixteen words, and
  that closed the shape question. Upstream documents the two status
  enums as having disjoint names, which would license the merge;
  `Enumerated` names one C++ enum, so a merged list cannot say which
  `from_word` a word belongs to. A test asserts the disjointness.

  It cost the codegen four things: the reader reads exception
  classes (it skipped every one, because an error declaration wears
  no decorator), an imported union brings its arms, `@spells` names a
  vocabulary a body uses and a signature does not, and the wire has
  an `error` kind pointing at the fault message it already had. Three
  claims were refuted by the build and are recorded there.

  The `Duration` alias landed last, with its first user.
  cpuUser/cpuSystem are `datetime.timedelta` through nanobind's own
  chrono caster, and an int of MICROSECONDS on the wire - both
  Carl's, and microseconds is lossless in both directions because it
  is a timedelta's own finest unit. `datetime.timedelta` is the first
  DOTTED type to cross; `pathlib.Path` is still blocked. A fourth
  claim was refuted, and it was mine: a spelling collapse written
  into `smoke_test._same` that no gate needed.
- 072 (a gate that has never tested anything) is DONE, by deleting
  it. `pyerrors.declared()` read `mod.classes`, which is empty for the
  errors declaration because the reader takes only DECORATED classes
  and an error class wears none. It turned out to have no caller in
  any commit - dead code shaped like a gate, which a reader counts as
  coverage. There is nothing left for it to check either: the module,
  the chain and the manifest come from ONE parse of one file, so they
  cannot disagree.
- 073 (a version-branched error class reaches nothing) is DONE, and
  it is what 072 found. `pyerrors` parsed the errors declaration a
  second time and got a RAW tree, so a class under `if NIX_VERSION
  >= ...` reached the emitted module unresolved and reached no
  manifest entry and no catch clause.

  Carl's call was to READ the branch rather than refuse it, and that
  needed no new machinery: the reader has resolved a version branch
  since it was written, so `read.resolved` and `Corpus.resolved` hand
  the same answer to an emitter that wants a tree. `pyerrors`'s three
  readings share one body now, and the emitted module drops the
  declaration language's imports because the arm is already chosen.

  Two gates, because the wiring and the mechanism fail differently.
  `_body` REFUSES a surviving `ast.If`, which is the only thing that
  can catch a revert to `Corpus.tree` - the two trees are identical
  for a declaration that does not branch, and none does.
  `tests/test_declarations.py` is new: the first suite that drives
  the reader and an emitter on declarations written for the test, so
  it can state a case the corpus does not have.

  Perturbed four ways, and the fourth is the positive one: a REAL
  class (`Unsupported`) moved behind a live branch, whole build run,
  236 tests and `check` green with it there.
- 068 (what a spike actually costs) is OPEN on its recommendations,
  and the second one is DONE: `nix-collect-garbage -d` on 2026-09-01
  freed 5.5 GiB across 22557 paths, 83% -> 77%. The jj workspace is
  still 2.5 MB and still deleted on exit, so the junk is the builds a
  worktree invites rather than the worktree. nix's own
  min-free/max-free are still unset, which is the one change that
  PREVENTS 062's failure rather than cleaning up after it.
- 064 (the manifest is a runtime interpreter) is DONE.
  `manifest.json` is deleted, and ONE of the two front doors is now
  emitted: `huggorm_bindings/__init__.py` comes off `cppgen/pyinit.py`
  and the package directory is empty in the checkout. The derivation
  reproduced the hand-written list exactly - twenty-five names, no
  special cases - with one forced subtraction: a free function a class
  names with `@produced(by=...)` is bound as `_ctor_from` and as no
  function of its own, so naming `open_store` on the front door fails
  to IMPORT rather than merely repeating itself.

  `huggorm/__init__.py` followed, on Carl's call: emitted into the
  store copy only, from a `setup.py` of its own. Forty-two names,
  again reproducing the hand-written list exactly, of which six are
  the hand-written layer's own API and cannot be derived from
  anything.

  The dev loop that cost is wider than it sounds and was measured: a
  directory with no `__init__.py` is a NAMESPACE portion, and Python
  prefers a regular package found later on the path - so the store's
  whole `huggorm` would win, not just its front door. `nix run test`
  copies the file into the tree first.

  One perturbation found an ungated decision that arrived with the
  change: dropping `RPC_CLASSES` from the plumbing filter passed
  every gate, because the front-door test only asked what reaches it
  and never what should not. It asserts both now.
- 065 (the declaration becomes the only source) is done, in four
  phases. `Corpus` reads each declaration once, the import resolves
  inheritance the tree cannot, and the emitted `_policy.py` carries
  what the JSON did.
- 066 (a declared error does not cross as itself) is done. A declared
  Nix error reaches the caller as itself on all three surfaces, so
  `except BadStorePath` works against the protocol and not only
  against the compiled binding.
- 050 (shim methods hand-type their signatures) is CLOSED, on a
  premise Cython took with it. Its steps 1-2 are scaffolding for a
  hand-written pyx, and there is no pyx or pxd left to scaffold; its
  "end state" section survives and is what the declaration arrived
  at. This line said PARKED until 2026-09-02, and the file had said
  CLOSED since 077 - the two disagreed, which is the drift the check
  at the top of CLAUDE.md exists to catch.
- 049 (ping resurrects dead connections) is done. Ping asks a lookup
  that does not create, answers ok=False, and takes its token from the
  metadata like every other rpc. The client stops rather than
  re-binding: a fresh bind would hand back a live-looking client whose
  every handle is dead.
- 048 (proto3 optional exists) is done. The schema emits the synthetic
  oneof, so an optional SCALAR has real presence and the codec reads
  every optional through HasField. PathInfo.ca proves both arms.
- 047 (enums disagree across the layers) is done. An enum is a scalar
  everywhere, including inside a container and inside a wire field.
- 046 (value types need dunders) is done for the dunders; the
  property question is deferred to the source-format spike, where its
  one real cost disappears.
- 045 (wire names before they freeze) is done. huggorm.v1,
  x-huggorm-conn, huggorm-* threads, and one casing for a method's
  two message names.
- 044 (the store as a graph) is done. references had one edge, one
  way, one path at a time; query_referrers is its inverse and
  compute_fs_closure is the transitive reading that makes either worth
  having. query_valid_derivers and query_valid_paths come with them.
  The enabling change is that a StorePathSet now crosses as base
  NAMES: it retired twenty lines of manual pointer ownership that
  would otherwise have been copied five times. It crosses as real
  store paths now - nanobind casts the std::set - but the reason the
  change was worth making stands.
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
  SUPERSEDED: all three are settled. 050 closed with Cython rather
  than being done, and a signature is stated once now - in the
  declaration.
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
above name are not Any to a typechecker (huggorm_bindings-stubs/, 027).
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
    _async       False to exclude         generation opt-out

    _abstract    True for a generated base       inheritance   (018)
    _blocking    False if no method can wait     wrap or not   (025)
    _produced    True if __init__ raises         constructible (041)

The PACKAGE declares none. It carried two - `_errors_module` and
`_async_twins` - and both were facts stated twice (065). Where the
errors module lands is decided by the emitter that writes it. A
word's ASYNC spelling sits beside its C++ one, in `declare.py`:

    Path = Annotated[pathlib.Path, Cxx("string"), Async("anyio.Path")]

`Async` maps pathlib.Path to anyio.Path: the binding returns the sync
type and the wrapper hands back the other, which is the one place the
two surfaces should differ. It never reaches the wire.

`_binds` was a tenth. It named the pxd declaration a pyx class bound,
which is a fact about Cython rather than about the binding, so the
nanobind emitter writes every other marker and not that one.

A class carries its markers in its BODY, which used to be forced: a
cdef class refuses any decorator but `functools.total_ordering` and
`dataclasses.dataclass`, and setting the attribute afterwards fails
because an extension type is immutable. It is no longer forced and
the shape did not change - the emitter writes `cls.attr("_threading")`
after the class, and a layer above reads the same thing.

The DECLARATION spells all of it with decorators, because a
declaration is plain Python:

    @binding(threading="pool")    execution policy, and more
    @wire_value(fields=...)       value, and how it serializes

Module-level functions take the same two:

    @threading("pool")            execution policy
    @binds("describe_store")      the C++ name, when it differs

They set the same attributes, so the generator reads one thing either
way. They return the function itself and never a wrapper: the
generator reads the signature off what the module exports, and a
wrapper would turn every parameter into Any - which the unresolved-type
gate catches.

Every public function is in the manifest either way; the policy
decides only whether it gets an async form and an rpc, and "pool" is
the only legal one - no instance, so no thread to be affine to (021).
An undecorated function says exactly that by carrying no decorator.

Constructor signatures come from the declaration, which is where they
were decided. They used to come from the pxd, because that was the
only place a Cython constructor's signature existed at all - Cython
exposes no signature for __cinit__ (019).
