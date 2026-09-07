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

- 035 (anyio, not asyncio) is DONE. A rule Carl set, and the
  conversion that follows it: two task groups in the server, and a
  client that owns one. `remote.connect` is an async context manager
  now, which is a deliberate break - Carl's answer to holding it as a
  question was to delete the sentence that made it one.
  The one exception is measured: the emitted runtime keeps
  `run_in_executor`, because anyio's worker pool cannot name a thread
  and an `EvalState` is affine.

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
- 012 (test blind spots) lost one of its four, 2026-09-06: "unknown
  CLASS on Acquire". The shape it named was already gone - the
  server-side lookup went when construction moved onto each class's
  own service, so an unknown class is an unknown gRPC path that
  grpclib answers before a handler runs. The CLIENT's check is what
  was left, and it was ungated on both branches.
  Folded into the gate that already drove one of them rather than
  written twice, and it now covers a DECLARED class that is still
  refused (`PathInfo`, which crosses as a value) beside an unknown
  name - either alone leaves "being declared is not what decides
  this" unstated. It also asserts the message names what IS
  acquirable; `match=` on the class name passed for a message that
  said nothing else.
  The arity check is gated at BOTH ends, and each half fails on its
  own perturbation. Recorded there: the first perturbation could not
  be "delete the branch", because the typechecker needs it to narrow
  `Acquire | None` - a check the typechecker will not let you delete
  still needs a gate, because it holds the shape and not the meaning.
- 008 (transitive policy), 022 (proto field
  stability), and the derivation half of 025 are hardening. 022 grew
  twice: free-function requests number their fields positionally too,
  and the manifest's "schema": 1 is written by the generator and read
  by nobody.
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
- 062 (the suite fills the disk) is DONE. Its own title is the wrong
  diagnosis, and the file keeps it and records the refutation.
  The 574 unexplained husks are a THREE-DAY lock timeout, not a leak:
  `keep=3` does not apply to a numbered directory whose `.lock` is
  younger than `LOCK_TIMEOUT` (`_pytest/pathlib.py:45`), and only a
  KILLED session leaves a lock behind. The MECHANISM is proved; that
  those 574 were all young-locked is what it predicts, not something
  measured - the evidence was deleted to reclaim the disk. Proved on
  a synthetic root -
  a dir with a half-day-old lock survives while one with a four-day
  lock, equally far below the window, is removed.
  Fixed by giving this suite its OWN basedir, because pytest's
  default is keyed by USER and not by project - the one fact behind
  every confusion in that file. `PYTEST_DEBUG_TEMPROOT` in the `test`
  runner, a FIXED path so the retention still prunes.
  562 MB per full run, measured in isolation for the first time, and
  1.7 GB retained. Acceptable, which answers the last open question.
  NO GATE, deliberately: the suite also runs in the build sandbox
  where the variable is unset and the default root is right, so a
  gate would have to skip when it broke. The runner prints the root
  instead.
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
- 083 (nothing decides when to forget) is DONE, watcher and source.
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
  with no sleeps.
  The source is `huggorm.Notifier`, over `asyncinotify` - Carl's
  choice, Linux only, and DEFERRED by him behind 033 and 032 until
  both were done. It is the third caller of `changed()`, which is
  what keeping noticing apart from bookkeeping bought.
  It watches parent DIRECTORIES, never files: a watch follows the
  inode, so an editor saving by rename orphans a file watch while the
  parent sees a MOVED_TO. Removing MOVED_TO from the mask fails the
  rename gate and nothing else. A directory event names every file in
  the directory, so a filter answers only the watched ones - dropping
  it fails two gates, both with `assert [] == [root]`.
  One assertion was decoration and is now a gate: "the marker is
  never a watched directory" holds whether the filter works or not,
  because a marker reaching add_watch raises OSError and sync() turns
  that into a change. The ROOT is what tells them apart.
  A directory that will not take a watch is a CHANGE, not a skip: the
  root is forgotten and `sync()` says so, because skipping would
  leave it cached against files that no longer exist.
  `CLOSE_WRITE` is deliberately not in the mask - it is one event per
  save rather than one per write, and zero for an mmap writer.
  Five of the seven gates read the WATCH SET and never wait; the two
  end-to-end ones wait on the EVENT under `anyio.fail_after`, which
  is why `next_change()` exists beside `run()`.
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
- 016 (evaluation server) is DONE: all three parts of its title are
  built and the destination milestone is gated. It has its warm cache.
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
  as a negative control.
  The watcher that decides WHEN is 083 and the eager pass over it is
  086, so nothing of the title is outstanding. Closing it does not
  settle the milestone's two CAVEATS - a handle id is the capability,
  so it shows the object survived rather than that the claim kept it
  (031); and "no re-evaluation" is shown by the file being gone
  rather than by counting evaluations. Both want a better instrument
  and neither is about whether the server exists.
  `resetFileCache` stays unbound, and for a better reason than the
  one it was deferred on: per-path invalidation made it a binding
  with no PURPOSE, because dropping the fetched flake inputs is
  exactly what a watcher must not do.
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
- 076 (an accessor that is an attribute) is OPEN, and one of its two
  questions is CLOSED 2026-09-06: a declaration writes `@property`
  OUTERMOST. Measured both ways - the marker over the property dies
  at import, the property over the marker reads with `prop` and
  `instant` both kept. The reader never cared: `_apply` runs the
  decorators against a throwaway and SKIPS the builtin ones, and
  `prop` comes from the tree.
  Said in a refusal, which is what the file asked for.
  `_descriptor_hint` reads the descriptor and the marker out of
  Python's own message and names the swap back - derived from the
  message shape, not from a list of markers. It is honest that the
  swap fixes only the IMPORT, because the emitter still refuses
  `@property`, and a hint that stopped short would send a reader to a
  second refusal with no warning.
  One gate, four assertions, three perturbations that hit three
  different asserts. The interesting one: with `_apply` applying
  `property` to its own probe, the marker lands UNDER a property
  object and is lost in SILENCE - in the arm the refusal now
  recommends. Nothing held that before.
  The first draft matched `staticmethod` and `classmethod` too, and
  both arms were DEAD: measured on 3.14.7, only a property refuses an
  attribute, so a marker over a `@staticmethod` imports and reaches
  `_method`'s refusal instead. Trimmed, and NO GATE drives the trim -
  an unreachable alternation changes no behaviour, so putting it back
  fails nothing. Said, rather than left looking covered.
  The rest of 076 is unchanged. `@property` in a
  declaration is refused, and the file says what honouring it would
  cost. Do it when a declaration needs an attribute, not for
  prettiness.
  Its estimate came DOWN when it was measured. Two of the five
  outputs it named need nothing: `pyi.py` already keeps `@property`,
  because it moves the declaration's own node and its filter keeps
  Python's own decorators - proven by reading, since `nbemit` refuses
  the class first and no property has ever reached the stub; and
  `wire.py` reads a value's parts
  through `_parts()` alone, so only the C++ that builds that body
  changes. The predicate is `nbemit.wire_fields`'s read expression,
  ONE place, plus `@shown`'s repr - and the open question is
  `manifest._method`, which carries no `prop` key, so nothing past
  the manifest can know. A wrapped class is where it bites: an async
  wrapper's method is `async def`, and a property cannot be awaited.
  `__call__` is NOT this task, and "one DSL change buys both" was
  the assumption that measurement refuted. See 088.

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
- 034 (functions as values) is MOSTLY DONE. Fourteen accessors on
  Value, all declared, plus `huggorm.signature_of` for the
  `inspect.Signature`. `apply` is the curried `f x` and `apply_auto`
  is `autoCallFunction`, by name, filling defaults.
  `@guard("function")` is NECESSARY AND NOT SUFFICIENT and that
  shapes the whole change: `nFunction` is one arm over THREE storage
  tags, so the guard proves the arm and reading a lambda payload off
  a builtin is still undefined. Every body checks its own shape, and
  one gate holds all twelve of those checks in both directions. NOT
  perturbed, deliberately: removing a sub-guard crashes rather than
  failing one assertion.
  `apply_auto` REFUSES a function with no formals, because upstream
  answers the function unapplied there and a caller cannot tell that
  from a call that returned a function. Broken on purpose: DID NOT
  RAISE, one gate.
  Three docstrings were written and then refuted by their own gates.
  The result of `apply` is in WHNF, not a thunk. Formals arrive in
  SYMBOL-ID order - interning order, because upstream sorts by a
  Symbol - so the bodies sort by name, the same answer this repo
  already gives for attribute sets; removing the sort fails one gate.
  And `doc()` on a lambda READS THE SOURCE FILE (position.cc:49), so
  it blocks - and throws from inside libexpr when that file has
  moved, which the binding catches narrowly and reports, because an
  empty answer would say "no documentation" where the truth is
  "cannot read it".
  No lambda gets an arity, because a curried function has none. Only
  a builtin declares one, read off PrimOp rather than getDoc - whose
  primop branch sits behind `if (primOp.doc)`, so a doc-less builtin
  has an arity getDoc will not report.
  `test_python_calls_nix_calling_python` is the gate neither 033 nor
  034 had: the two are duals with opposite threading, and it crosses
  both in one call so the GIL reacquire happens inside the release.
  Left: `__call__`, which is a DSL gap tracked in 088.
  The cross-state argument is CHECKED now, and 085 holds it: Carl
  decided a state is its own isolation, so `apply`, `force`,
  `apply_auto`, `list_append` and `attrs_set` all refuse a value from
  another state - from one check in `BaseRunner.call` rather than
  seven.
  The remote gate arrived by accident.
  `test_a_proxy_argument_resolves_against_another_object` used two
  states to prove a handle resolves from the table; that call is now
  forbidden, so it was rewritten onto `fn.apply(arg)` - two Values of
  one state being the only way two proxies can legally meet.
- 033 (primops in Python) is DONE. `EvalState.register_primop`
  publishes a Python callable as `builtins.<name>`: `fun<PrimOpFun>`
  holds a std::function so a capturing lambda goes straight in, and
  the Reach shim that reached `fileEvalCache` reaches the private
  `addPrimOp` unchanged. Two of the file's own premises were wrong -
  there are no trampolines since 060, and arguments DO outlive the
  call safely because every Bridge roots. Building it found that
  upstream never sorts after `addPrimOp` (createBaseEnv sorts once at
  the end), so a registered name could not be found while the
  suggestion engine still saw it; nine of ten gates passed without
  the fix. It needs the carried base-env patch, whose first real gate
  this is.
- 032 (log callbacks) is DONE. The in-process half works:
  `EvalState.subscribe_logs` hands back a bounded `LogStream` a reader
  drains, and a record is Nix's own - `action`, `level`, `id`,
  `parent`, `type`, `text`, `fields` - rather than a line of text. A
  queue and not a callback, because a callback would take the GIL once
  per log line inside the evaluator. The bound refuses a `msg` and a
  `result` and never a `stop`, since a dropped stop leaks a node in
  the reader's activity tree forever.
  It measured two things that shape it. The global `nix::verbosity`
  filters before any logger runs, so a subscription can only narrow;
  and activities are not filtered at all, so a start arrives whatever
  its level says. It routes by THREAD, which is sound because this Nix
  has no parallel evaluation - and leaves fetcher threads uncovered,
  which is named rather than fixed.
  It found the emitter publishing HALF a surface: `subscribe_logs`
  crossed as an rpc answering a `LogStream` handle that no service
  could take, because `annotate` assumed an unwrapped class crosses by
  copy - true of every unwrapped class until this one, all of which
  were wire values. And `ASYNC_CLASS` named an `AsyncLogStream` that
  does not exist. Both are refused now by a RULE rather than a
  blocklist: a return whose type is a proxy with no service is a
  wire_blocker.
  The tap is a hand-written `nix::Logger` subclass, approved by Carl
  on 2026-09-03, and `tasks/084` holds the DSL gap that made it
  hand-written.
  The second half is `Session/Logs`, a server-streaming rpc written
  beside Session because it is protocol: every other rpc is the wire
  form of a declared method and this is the wire form of none. A
  message is one DRAIN, and `dropped` rides with every one of them.
  The FIRST batch is empty and means "subscribed" - without it a
  caller cannot know when starting the work it wants logs for is safe.
  A second reader on one state is REFUSED, because a second subscribe
  replaces the first in the C++ and the older stream would go silent.
  It polls at 50ms rather than waiting on a condvar, which would be
  new hand-written C++ for a speed that is not a goal yet.
  Building it found three things. "A `finally` cannot await under
  cancellation" was ARGUED and then refuted: two perturbations that
  should have failed both passed, so the synchronous-first cleanup is
  a cheap defence rather than a measured necessity - the pop itself is
  gated and its ordering is not. The Session guard wrapped a
  deliberate refusal in InternalError and threw its status away. And
  `server_streaming` had never been set in this schema, so a
  descriptor that lied would have had nothing to notice it.
  Two gates were wrong before they were right: a drop test raced the
  server's own 50ms poll and passed five times anyway, and the swept
  branch was claimed as a decision before anything ran it. Both are
  fixed and both failures are written out.
  Four residues moved to `tasks/085`.
- 086 (nothing evaluates before it is asked) is DONE. `huggorm.Warmer`
  re-evaluates registered roots the watcher dropped, which is the last
  third of 016 and the last piece of the destination.
  `stale()` is DERIVED, and that is the design: `changed()` deletes a
  forgotten root's snapshot, so "registered and has no snapshot"
  already means "its answer is gone" - the module records no
  staleness and so cannot disagree with the watcher.
  A failed eager pass is RECORDED and left stale, never raised. An
  eager pass has no caller to tell, and a half-saved file is the
  normal case; stale is the half that matters, because a failure
  marking the root warm would leave it never re-evaluated.
  The DEBOUNCE was wrong twice and the sequence is worth keeping. It
  was argued; a measurement then said it was pointless, because a
  drain loop across a save saw one change - the first event forgets
  the root and empties `watching()`. That probe never REFRESHED
  between events, and a refresh re-watches the files, which is what
  lets a straggler through. The gate failed with two evaluations for
  one save and the window went back in. A real measurement, in the
  wrong harness.
  It does not make a user's call faster while it runs: an EvalState
  is affine, so the wait is one eager evaluation. Not zero, and only
  a second EvalState would make it zero.
  The file first closed saying the destination SENTENCE still needed
  an end-to-end gate. It does not: `test_lifecycle.py`'s
  `test_a_claimed_state_answers_for_a_file_it_can_no_longer_read` is
  that gate and predates this work. Its technique is the better one -
  the creator deletes the file it evaluated, so a cache miss cannot
  be faked, and a fresh EvalState in the same test is the control.
- 087 (a failed evaluation wedged its root) is DONE, and is the fifth
  instance of the named failure mode - the first that is not an
  emitter skipping something. libexpr caches what a FAILED evaluation
  read, so a file fixed after a bad save keeps raising the OLD error;
  and a failed evaluation records no snapshot, so no later change can
  implicate that root. Permanent, and a confident wrong answer rather
  than an absence. 083's seven gates all evaluate files that parse.
  `Watcher.eval_file` forgets what a failure cached, DERIVED at no
  extra call: cached and in no snapshot means read by an evaluation
  that failed. It errs towards forgetting, because forgetting a good
  file costs a re-read and keeping a bad one costs the answer. Two
  gates, at two layers, and reverting fails both.
- 088 (a declared dunder reaches nothing) is OPEN, with the refusal
  DONE. The SIXTH silent skip: `read.py`'s class-body loop kept the
  names that are not `__`-prefixed and dropped the rest with no
  answer, so a declaration writing `def __call__` reached no binding,
  no stub line and no manifest entry, and the read reported success.
  Measured on a probe: two declared methods gone, `Probe
  ['nar_size']`. No declaration tripped it - `errors.py` declares
  `__eq__` and `__hash__`, and an error class carries no `@binding`,
  so that file yields no bound class at all.
  The branch is inverted now, refusing through `_survive` with the
  line and both answers a caller has: the value dunders are DERIVED
  from `@wire_value` - `order=` and `text=` decide which - and
  anything else gets a plain name. Broken on purpose: DID NOT RAISE, one gate.
  The `async def` half is DONE too. An `async def` in a declaration
  died in 082's reconcile blaming `co_firstlineno`, which never says
  "async" and sends a reader to the wrong file. Three readers ask
  which nodes DEFINE a name and two spelled it out as
  `ClassDef | FunctionDef`, so `DEFINITIONS` states it once - and
  each loop that drops one now says a declaration describes a C++
  binding, where the async form is derived from `@threading` and
  `@blocks`. An UNDECORATED module-level `async def` is left alone,
  like an undecorated `def`, and could not be written at all before.
  Three gates, and dropping `DEFINITIONS` fails all three rather
  than the one its docstring predicted: reconcile runs first, so the
  refusals are unreachable without it. One fixture also passed for
  the wrong reason - three asyncs in one file let the class test's
  regex be satisfied by the free function's refusal - so each
  fixture carries one.
  What is left is `__call__` itself, which is `tasks/034`'s residue.
  It is NOT 076's problem, and "one DSL change buys both" was the
  carried assumption that this refuted: `@property` is
  attribute-versus-call in four emitters, and `__call__` is a name the
  reader never kept. `nbemit` and `pyi.py` need nothing; the manifest
  is a SECOND silent-drop layer with its own `startswith("_")`
  filter; and a proto identifier must start with a letter, so
  `__call__` cannot be an rpc name.
- 085 (four things the log stream does not cover) is OPEN, with its
  FIRST gap done: `huggorm.subscribe_process_logs`, for records
  raised on a fetcher, a file-transfer thread or a build. Carl
  approved the C++ by name; `eval.hpp` grew 104 lines, about 42 of
  them code.
  A FALLBACK, not a broadcast. A thread that subscribed CLAIMS its
  records, so the sink answers what nobody claimed rather than
  everything in the process. Broadcasting would buy the second
  question and cost a caller holding both subscriptions every record
  twice, with nothing on a LogRecord to deduplicate by - so the shape
  that answers "everything" is fan-out, which is gap 3, still open.
  Two gates, one per direction, because either alone passes for the
  wrong reason. Removing the fallback fails both.
  The scoping question 032 opened is answered at TWO layers on
  purpose: the C++ REPLACES, because refusing there wedges the sink
  when a caller drops its LogStream; the rpc REFUSES, because a
  stream ending releases it.
  It needs a MUTEX where `thread_queue` needs none - that slot is one
  thread's, this one is read from every thread while another writes
  it - and the replaced queue is closed outside it, because close
  takes the queue's own lock.
  Stated rather than fixed: the never-drop-a-stop rule was justified
  by a per-state queue being bounded by ONE EVALUATION, and a
  process-wide queue in a service that runs for days is bounded by
  the reader instead.
  The codegen needed NO change for a shape it had never seen - a free
  function returning a proxy with no service. Every emitter derived
  the same answer, including the refusal to give it an rpc, and its
  inverse got one for the same reason `unsubscribe_logs` has one.
  The rpc is `Session/ProcessLogs`, hand-written beside `Session/Logs`
  and `client.process_logs()` on the far side. `ProcessLogsReq` is
  `LogsReq` without the handle - the only structural difference - and
  they SHARE `LogsResp`, because a batch and a drop count is the whole
  answer either way. `_options`, `_pump` and `_log_stream` are the
  three things written once rather than twice.
  It REFUSES a second stream, which is where the sharing question
  lands. A BOOL and not a map, because there is nothing to key one
  sink by - so it is one reader for the whole SERVER, not one per
  connection, and fan-out is what would change that.
  `test_the_descriptor_says_it_streams` caught the schema change
  itself, failing with `{'Logs', 'ProcessLogs'}` - an exact-set
  assertion earning its keep. Three perturbations: no fallback fails
  three gates, no refusal fails one with DID NOT RAISE, and no flag
  clear fails three, because a wedged server poisons the tests after
  it.
  Gap 2 is DONE too, by a RULE rather than a fix. Carl: one state,
  one thread, and a value of one state is not valid in another unless
  it is forced to data and copied. So "two states on one thread share
  a subscription" is a caller doing what the design forbids, not a
  limitation to route around.
  The thread half needed no code: `AffineRunner` builds its own
  `max_workers=1` executor, so two states cannot share a thread. One
  gate asserts it.
  The VALUE half is `BaseRunner.call`, checking every argument before
  the executor hop - one place covering all seven `Value` parameters
  across `force`, `apply`, `apply_auto`, `list_append` and
  `attrs_set`. That also closes 034's cross-state residue. The
  EXECUTOR is the identity, not the runner, because a value gets an
  AttachedRunner over its producer's.
  In `call` and NOT in `unwrap_arg`, after a measurement: `_invoke`
  wraps what it catches, so the refusal arrived as
  `InternalError: force failed` with the reason buried in a cause.
  Enforced in the async layer, and Carl chose that: it is the lowest
  layer that manages threads for a caller, the rpc goes through the
  same wrappers, and a sync caller is on their own. Goal 1 is not in
  tension - libexpr has no such rule, so the binding is exactly as
  permissive as what it binds.
  Two tests in this repo were making the forbidden call incidentally,
  both with a different subject, and both were rewritten rather than
  exempted. The `smoke_test` one also turned out to be claiming
  coverage it never had.
  The wire-value exemption is a DEAD BRANCH - removing it fails
  nothing, because every wire value comes from a pool class - and it
  is marked unreachable rather than left looking covered.
  Perturbing the check fails exactly three gates; the control, the
  thread gate and the pool gate all still pass.
  Gap 3's RATIONALE is refuted, and 089 records it. It argued fan-out
  was "a plausible want and not an observed one"; Carl observed it - a
  CLI wants a global listener printing to stdout as things happen. The
  gap is still open, but it is now required rather than speculative,
  and nanopynix shows it needs no C++.
  Gap 4 is unchanged: the ErrorInfo overlap with 036.
- 091 (how much of the C++ could the DSL say) is OPEN. REMEASURED
  2026-09-06: `cpp/` went from 593 code lines to 793 and
  `logging.hpp` from 208 to 356, which is 45% of the total and the
  number Carl's question was about.
  About 100 of the 148 new lines are NOT derivable by this file's own
  test - `VerbosityDemand` is a counted registry, `ThreadLevel` is a
  lifetime rule with a deleted copy assignment, `set_process_demand`
  is a pairing under a mutex. The file grew because 089, 095 and 096
  were concurrency work, not because the codegen fell behind.
  Two figures move: 084's `LogTap` is 75 lines, not 62, so with the
  32 it unblocks it is over 100. And "a typed slot, three times" is
  now SIX times, 30 lines - the second-cheapest derivable item, and
  blocked by 084 for the same reason everything in that file is.
  It answers
  Carl's "I'm surprised there's so much C++". About a THIRD, not most.
  1545 lines, of which 593 are code - the rest is 828 comment and 124
  blank, which is deliberate here and does inflate what a reader sees.
  ~200 lines are shape a declaration could carry: the `Reach` dance
  (17, three identical users), `LogTap`'s five overrides (62, which is
  084), the four subscribe/unsubscribe functions (41, one shape four
  times), the two log structs (16, every field already named by
  `@reads`), and six smaller patterns.
  ~300 are algorithms goal 2 explicitly ALLOWS: `Bridge`'s root
  lifetime (106), `LogQueue`'s action-dependent drop policy (52),
  `register_primop`'s re-entry into Python (45), the GC thread dance
  (31, justified by a measurement). Shrinking those would trade a
  readable queue for a declaration form with one user.
  The real finding is the PATTERN. `@property`, `@staticmethod`,
  `@classmethod`, a dunder, `async def` and a virtual are all refused
  or unsayable - six, five of them open, and one habit rather than six
  gaps. Refusing was right each time, and six is where it stops being
  the answer.
  The DEPENDENCY test it applied to `@private_member` applies to the
  list, and it did not say so. Grepped every consumer: the three
  slots and the two log structs are used only by `LogTap`, which is
  hand-written and in the same file, so 32 of the derivable lines are
  blocked for the identical reason. The four subscribe functions are
  called only from `Cxx` bodies, so 41 are not. That makes 084 the
  UNLOCK rather than merely the largest item.
  Moving the unblocked 41 into `Cxx` bodies is a RELOCATION and not a
  derivation, so the census is not a reason to do it.
  It first claimed `CLAUDE.md:95` was stale - that nothing prints the
  `huggorm_decl/cpp` line count. WRONG, and corrected in the file:
  `census_cpp` prints `hand-written C++ in cpp/: 593 lines in 5
  file(s)` on every build, and names the three ORPHANS - files no
  module claims, `logging.hpp` the largest at 208. That is the third
  claim of this shape in one session and the worst, because it was
  committed and accused the instructions of being wrong. The number
  reported is CODE lines, so the "comment ratio inflates it" answer
  was wrong too.
- 090 (are all the lines in the headers justified) is DONE.
  Carl asked; the answer was NO. Ten lines, all left behind by 089's
  split an hour earlier - seven includes in `eval.hpp` whose users had
  moved out, two in `gc.hpp` named only in comments, and a
  `class Bridge;` forward declaration that is load-bearing in
  `eval.hpp` and dead in `gc.hpp`. Found by asking each file which
  symbols it names OUTSIDE a comment, which is the part a plain grep
  gets wrong.
  `<stdexcept>` stays and the reason first given for it was FALSE.
  `eval.hpp` uses nothing from it; the EMITTED file throws
  `std::invalid_argument` 54 times. The comment claimed the build
  breaks without it, and it does not - `logging.hpp` reaches
  `nix/util/error.hh`, which supplies the name. It stays because that
  chain is an accident, not because it is load-bearing.
  A second pass over the other three files found more. `errors.hpp`
  opens by describing ordered catches THAT ARE NOT IN IT - the
  translator is emitted, and this file is the two helpers it calls -
  and three of its four nix includes serve the emitted files rather
  than itself. Predicted removing them would break the build; it did
  not, which is the SECOND wrong prediction of that shape in one
  session. They stay because `path.cpp` catches a store-api type while
  including only `path.hh`, so keeping them is a weaker accident than
  the alternative.
  HALF of that is DONE: `nbemit.BODY_HEADERS` derives a unit's
  standard headers from what its `Cxx` bodies SPELL, so `eval.cpp`
  gets `<stdexcept>` because its bodies throw and `eval.hpp` loses the
  one it never used. Neither perturbation breaks the BUILD - nix's
  headers reach `<stdexcept>` anyway - so the gate is on the emitted
  TEXT, with `pathinfo.cpp` as the control that gets `<cstdint>` and
  not `<stdexcept>`. Removing the derivation fails it and only it.
  The other half is DONE too, on 2026-09-05. `decl/errors.py` says
  `header = "nix/..."` beside each `cxx`, the emitter writes that
  include into every unit with a translator, and `errors.hpp` keeps
  the one nix header it uses itself. Refused BOTH ways: a `cxx` with
  no `header` fails the build, and a `header` with no `cxx` is a line
  no emitter reads.
  That audit found a LIVE defect. `pyerrors.module` stripped `cxx` in
  place, on the tree `corpus()` caches for the whole process, so any
  reader after it saw a declaration with no C++ in it - an empty catch
  chain, which compiles and turns every nix error into a
  `RuntimeError` in silence. It had never fired only because
  `generate.py` calls `error_chain()` first. Copies now, and the gate
  is chain-module-chain.
  Two things in `logging.hpp` may still be derivable and both belong
  elsewhere: `LogTap`'s five overrides are `tasks/084` exactly, the
  one MAPPING left in the headers; and `LogField`/`LogRecord` are
  plain structs whose every field the declaration already names with
  `@reads`.
- 089 (correlating a log with the call that caused it) is DONE, all
  four steps. It began as a REFLECTION rather than a plan - Carl asked what nanopynix does
  about a per-request log id and granular verbosity, and how either
  fits here. Nothing is implemented.
  nanopynix keeps a `thread_local` request id that its logger passes
  as the first argument of every callback, set and restored at one
  dispatch chokepoint; a `request_finalized` CONTROL event that is
  never dropped, because an id without it says which call a record
  belongs to and never says the call is done; a `thread_local`
  verbosity with `nix::verbosity` pinned wide open, because the global
  is a non-atomic that ThreadSanitizer catches; and per-OBJECT
  ownership of the level.
  Three structural claims were re-read against 2.34.8 rather than
  believed: `printMsg` gates lazily on the global, `Activity::Activity`
  calls `startActivity` unconditionally, and libstore holds 138
  `debug()` sites against their 139. Their TIMING table is not
  adopted - the `chatty` ceiling was measured on their workloads.
  The conflict this repo has and they do not: huggorm's tap is a TEE
  that keeps the old logger as MAIN, so pinning the global open would
  leave the console arm unfiltered while ours filters. That decision
  comes before everything else in the file.
  A request id also does NOT cover what the process sink exists for:
  Nix's own threads never pass a dispatch wrapper, so a build's output
  carries no id. The activity `parent` chain is the cross-thread half,
  and it is already on the record.
  DECIDED since: REPLACE the logger, do not tee. Carl, for the stdio
  transport - the protocol will run over stdin/stdout and nothing may
  log there. Read rather than assumed, four things the tee gives:
  console output goes to STDERR already (`SimpleLogger` ends in
  `writeToStderr`), so a fallback keeps it; `writeToStdout` writes to
  descriptor 1 directly and every caller in 2.34.8 is in the CLI, none
  in libexpr or libstore, so override it anyway; `ask` already returns
  nothing; the rest have empty bases.
  And a correction to the reason: Carl's own `grpclib-transports`
  already defends descriptor 1 STRUCTURALLY - `take_wire_descriptors`
  makes fd 1 a duplicate of fd 2 before anything writes, so a stray
  write from libnix is a log line rather than a corrupt H2 frame. So
  stdout safety is not the argument. OWNERSHIP is: a tee leaves
  `SimpleLogger` filtering on the global with no way for a caller to
  narrow it, which makes per-thread verbosity unimplementable while
  the tee stands.
  `subscribe_logs`'s "can only narrow" sentence becomes FALSE under
  this, and has to change in the same commit as the pinning.
  STEP 3 IS WRITTEN. A record carries `request`, the call it was
  raised inside, and an `end_request` pushes a `"finalized"` marker
  saying that call raised its last one. Carl approved the four C++
  lines it needed; the callable pair is `Cxx()` bodies in the
  declaration, carrying NO threading policy - that is the
  `gc_release_thread` lane, and a declared one would have hopped to
  the pool and set the thread_local on the wrong thread, silently.
  The id is per CALL and Python allocates it. A caller-supplied one
  was rejected: a request spans calls, so a marker per call would
  fire many times for one id. So a reader GROUPS by the number and
  learns the group is closed; it cannot ask for the logs of a call it
  just made. That is additive and is not done.
  Both drop policies - `LogQueue::push` and `_Reader.offer` - name
  what to DROP by membership, so the marker inherited never-dropped
  on both layers. Checked before designing on it: written the other
  way round, this was the eighth silent skip.
  Two claims in the file were REFUTED by writing it. The zero case is
  a gate after all (the sync binding never sets an id, so its records
  carry 0), and a marker changes what an existing reader sees - a
  capacity-1 gate that asserted `len(records) == 1` now counts
  messages instead.
  Both gates were proved by BREAKING them: deleting the stamp fails
  the two id gates and nothing else, and making `"finalized"`
  droppable fails the marker gate with `got 1` - the one marker
  `subscribe_logs` pushed into an empty queue, which is the right
  reason rather than zero.
  A "434 tests" written into 089 was NOT measured - the run passed
  `-q` and printed no totals line. The real one is 396 passed, 10
  deselected. Recorded rather than corrected quietly.
  STEP 4 IS DONE. `install_log_tap` pins `nix::verbosity` wide open
  and `effective_verbosity()` decides instead - a `ThreadLevel
  {own, level}` per thread over a `std::atomic<int>` process default.
  Two fields, and the flag is load-bearing: a `thread_local`
  initialiser runs once, so a fetcher thread that started before a
  caller raised the default would hold the old value forever.
  `subscribe_logs` sets the thread's level, `subscribe_process_logs`
  raises the default - it has to, or its own level is a lie, because
  the records it exists for come from threads that never set one.
  NO PIN, and a draft had one - `nix::verbosity = lvlVomit` at
  import, copying nanopynix. A review asked who else READS that
  global: `RemoteStore::setOptions` sends it to the daemon
  (`remote-store.cc:118`) and `daemon.cc:239` assigns it there. So the
  pin asked every daemon connection to narrate at vomit down the
  socket, forever. All 412 tests passed over `dummy://`, which opens
  no daemon connection.
  `raise_verbosity` instead, monotonic: nix's gate moves only when a
  caller asks, which is what `-vvv` does. The trade is stated - a
  race on a plain global where the pin had none, against a flood that
  is observable and permanent and hits a machine this process does
  not own.
  It needed a gate, and that needed a READER: `process_verbosity()`
  is plumbing-lane surface added because an invariant nothing can
  observe is one that gets broken. Restoring the pin fails exactly
  that gate.
  The SERVER was asking for everything. `LOG_LEVEL_ALL = 7`, with the
  destroyed rationale in its own comment - "asking wide costs nothing
  real, because the global filters BEFORE any logger runs". Both log
  rpcs used it, so either would have pinned the daemon through the
  front door. Replaced by `_widest(readers)`, and a `_Fanout` reopens
  when a joiner wants more, which keeps the arrival-order fairness
  the constant protected.
  The workload was MEASURED after a draft guessed wrong. A trace is
  lvlError and a trivial `eval_expr` raises nothing under lvlInfo at
  all; `evaluating file` is lvlTALKATIVE, from `eval_file`. So the
  gates evaluate a file.
  `LogQueue::level_` IS GONE, which is the "two filters on one axis"
  question answered by measurement: perturbing the tap's gate failed
  only ONE test, because the queue caught what the tap let through.
  Nothing can reach a queue the thread's level did not admit. With the
  duplicate removed the same perturbation fails THREE - two were
  passing for the wrong reason.
  414 passed. The totals line took two runs to read, because the
  first passed `-q`: the same mistake this task already records from
  step 3, made again in the same session.
- 093 (a python primop that closes over its state leaks the state) is
  DONE. It was a DEFECT, not the shutdown warning it was opened as.
  Bisected to exactly four tests, one leak each, and the discriminator
  is what the registered callable closes over: every leaking one names
  `state`, every clean one does not. Four other tests register primops
  and are clean.
  `register_primop` captures the callable as a strong `nb::object`
  inside a lambda that lives in the state's base env, so the cycle
  runs wrapper -> EvalCore -> nix::EvalState -> PrimOp -> nb::object
  -> closure cell -> wrapper. The `weak_ptr<EvalCore>` breaks the C++
  arm; the arm that closes it is the Python reference held from inside
  nix's memory, which Python's collector cannot traverse. Two
  `gc.collect()` calls do not free it, so it is UNCOLLECTABLE.
  It matters because closing over the state is the NATURAL way to
  write a primop - the result has to come from `state.make_int` - so
  the useful callables are exactly the leaking ones, and a service
  that runs for days can never reclaim such a state.
  One probe LIED and is recorded: it did `del state` first and read
  "no leak" as "no cycle". The lambda shares that binding through a
  cell, so `del` emptied the cell and broke the cycle by hand.
  FIXED. The core now owns the callable and the lambda carries an
  index, so one strong reference sits where a GC slot can reach it.
  `evaluator_tp_traverse` and `evaluator_tp_clear` are hand-written in
  `cpp/eval.hpp` - nanobind's own `refleaks.rst` says it offers no
  abstraction - and a new `@gc_slots("huggorm::evaluator_slots")`
  makes the emitted `nb::class_` pass `nb::type_slots(...)`. That is
  the helper shape Carl described the same day: manual C++ the emitter
  BINDS TO.
  A destructor drops the callables with the GIL held, because the
  core's deleter can run on a pool worker or the reaper - which is
  what `make_core` exists for - and only a Python finalizer holds it.
  4 leaked instances before, 0 after, 408 passed.
  The gate is a CANARY in the closure, not a weakref: the bound type
  has no weakref slot. It calls no `del`, because `del` would empty
  the cell and break the cycle by hand.
  `tp_clear` is NOT covered by it, measured rather than assumed:
  releasing nothing there changes nothing, because CPython needs
  traverse on every type in a cycle and clear on only one, and a
  function object and a cell carry their own. Kept as correctness and
  recorded as untested.
- 095 (what a raised verbosity costs) is DONE. Measured over the
  daemon store, four conditions, 12 rotated repetitions.
  The CLIENT-side formatting cost is not measurable: the condition
  that raises after the handshake is the baseline within noise on all
  three phases, which confirms 089's count of the sites.
  The DAEMON-side cost is 1.05x to 1.46x, because
  `RemoteStore::setOptions` sends the level and the daemon then
  narrates every worker op back over the socket. Writing to stderr is
  not the cost - a condition that queues the records instead is no
  faster.
  The probe also found what it was not looking for, and 096 is that:
  a process that subscribed once prints daemon debug lines on stderr
  forever, because `worker-protocol-connection.cc:75` re-raises every
  daemon line with `printError` and ERASES its level.
  It refuted a comment in `logging.hpp` claiming an unsubscribed
  caller sees what it saw before the tap existed.
- 096 (a raised global narrates forever) is DONE. `VerbosityDemand`
  counts holders per level and sets `nix::verbosity` to the widest
  live one, with the floor read once rather than written as lvlInfo.
  A COUNT rather than a maximum, because a maximum cannot be undone.
  `ThreadLevel` owns the pairing and gives the level back from its
  destructor, so a thread that exits while subscribed does not hold
  the gate up.
  It got that wrong first. A destructor on a plain aggregate turned
  `chosen = {.own = true, ...}` into add-then-drop, because the
  TEMPORARY is destroyed after the copy assignment - so every
  per-thread subscription silently failed to raise the gate. Three
  gates caught it, one of them 089's. The copy assignment is deleted
  now, so the compiler rejects the line that was wrong.
  Three gates, each proved by its own perturbation, and the LIVE one
  fails with the defect's own text on captured stderr. It uses
  `capfd`: the write is a C++ `writeToStderr`, and `capsys` only
  replaces Python's objects.
  One thing lowering cannot reach, and the docstring says so: a
  connection already open keeps the level `setOptions` gave it.
- 094 (nothing catches a missing @gc_slots) is DONE.
  `census_gc_slots` prints on every build, beside `census_cpp` and
  `census_markers`. PER FILE, which is a limit rather than a
  shortcut: the `Evaluator` does not hold the callables itself -
  `EvalCore` does, and nothing binds `EvalCore` - so a per-class check
  would follow C++ members through a type no declaration mentions.
  It would have caught the original.
  The member test is text, not a parse, and the clause that does the
  work was measured: a line carrying `nb::object` and ending in `;`
  matched the member AND the wrapped tail of `register_primop`'s
  declaration, so a line with any parenthesis is excluded.
  Proved by breaking it. Removing the marker prints TWO reports -
  `census_markers` says nothing carries it, this says which file
  needs it - and both are kept, because either alone reads as noise
  and together they name the fix.
- 084 (a declaration cannot implement a virtual) is OPEN and blocks
  nothing. It got HARDER, 2026-09-06, and the file says so.
  It was five `LogTap` overrides of one shape - build a record, route
  it - which is what an emitter is for. There are SEVEN now, in four
  shapes: `log` gates then routes, `logEI` gates and RENDERS,
  `startActivity` routes unconditionally and gates only its fallback,
  `writeToStdout` delegates, `isVerbose` builds no record at all.
  Only `stopActivity` and `result` still read as the original shape.
  So the uniformity premise the case rested on is gone, and a
  declaration form would need per-override syntax for a gate and a
  transformation - for one class. The ONE-implementer reason to wait
  now has a second reason beside it.
  Two more facts in that file were stale and are corrected: `LogTap`
  is in `cpp/logging.hpp`, not `eval.hpp`, and `install_log_tap` no
  longer uses `makeTeeLogger` - 089 step 4 made it a replacement.
  Still not a mapping, and the 32-line unlock is still real.
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
- 014 (transport shims) is the destination that is left. 016
  (evaluation server) is DONE: the lifecycle contract, the warm
  caches, the file graph and background evaluation are all built, and
  `CLAUDE.md` now describes something that exists rather than
  something aimed at. What follows it is ordinary work on a running
  service - the residues in 085, the transports in 014, and whatever
  the first real caller finds.

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
