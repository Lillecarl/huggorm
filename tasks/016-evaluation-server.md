# Evaluation server: persistent state, watched files, background eval

**OPEN.** The lifetime contract, the warm cache and per-path
invalidation landed. No inotify watcher, no background evaluation.

This is the project's DESTINATION, and it is stated as one in
`CLAUDE.md` under "What this is for" - including the milestone that
counts as reaching it: a second client claims a live EvalState, and
re-evaluating unchanged input does no re-evaluation. This file used to
be the only place the vision was written down, which is why nothing
above it steered by it.

Carl's vision (2026-08-23). The remote layer stops being a method
façade and becomes a long-lived evaluation service:

- One EvalState serves many connections over days. Clients create,
  Detach (see 002), exit; later clients Claim the same state. No
  reevaluation of unchanged input.
- Repeated evals run against warm caches on that state - fast AND
  correct Nix-direnv-style work.
- Server watches every non-Store file an evaluation touched
  (inotify). Watched expressions can be registered for background
  eager evaluation; next user-triggered eval is progressing or already
  done instead of starting cold.

Depends on 002 (escrowed handles outliving creators). Real value
needs the real libexpr (015) - the mock has no true evaluator or
file dependency graph to watch. Design the lifecycle contract here;
build after 015 lands.

## 2026-08-25: the lifecycle contract, as an executable claim

"Design the lifecycle contract here" is done, and written as a test
rather than a document - test_lifecycle now drives the whole handover:

    a client acquires an EvalState, does work, detaches everything and
    stops pinging; the sweeper reaps its connection; a later client
    presents the token, and finds the same evaluator with its work
    intact and still able to evaluate.

Forcing is what makes "warm" checkable. It mutates a value in place,
so a value that reads as an int on the far side of a handover is the
one that was forced before it - not a rebuilt copy. A whole attribute
set built before the handover comes back through Realize afterwards.

What the test does NOT prove, and it is worth knowing which: that the
CLAIM is what kept the objects reachable. A handle id is the access
capability, so a successor that knows the ids could call through them
without claiming anything (tasks/031). The claim is what makes it an
OWNER - the leases move out of escrow onto its connection, so its
release is a real release and its silence is a real death. The token
check pins that half; the release-after-claim path is covered by the
detach section above it.

Everything above is against the mock, which has no evaluator to keep
warm in any meaningful sense: nothing is cached, so "the same state"
means the same object rather than the same work avoided. Warm caches,
the inotify file graph and background eager evaluation all still wait
on tasks/015. The lifetime contract they will need does not.

## 2026-09-02: the warm cache is reachable, and it is state-local

`eval_file` is declared. It was the missing half of "warm": libexpr
caches an evaluation by RESOLVED PATH, and nothing in this binding
took a path.

    EvalState::evalFile (eval.cc:1118)
      resolvedPath <- importResolutionCache, or resolveExprPath
      if fileEvalCache has it: forceValue, copy, RETURN
      else: parse, thunk, force, and put it in the cache

Read from Nix's own source rather than assumed. Two things follow.
A hit never opens the file. And `eval_expr` has no cache at all - a
string is not a key - so every warm claim this project makes is about
files.

### The gate, and its own negative control

`test_a_file_evaluated_twice_is_read_once`, on all three surfaces.

Evaluate a file, DELETE it, evaluate the same path again. The second
call is answered, because a cache hit never goes to disk. Then open a
fresh state and ask it for the same path, in the same moment, with
the file still gone:

    SysError: error: opening file
    '.../answer.nix': No such file or directory

Identical on sync, async and rpc. That message is the control: it is
the cold state SAYING it went to disk, which is the half the warm
call is claimed not to do. The test needs no timing and no counter.

An int, not an attribute set. `evalFile` forces to WHNF, so a value
that is not complete there leaves a thunk that may still want the
file - and a failure would then mean the wrong thing.

### What it says about the milestone

`CLAUDE.md` names the milestone: a second client claims a live
EvalState, and re-evaluating unchanged input does no re-evaluation.
The third part of this gate is the reason that wording is right. The
cache belongs to the STATE. A fresh evaluator reads; a state that
dies takes the warm work with it. So the handover is not a
convenience over restarting - restarting is what loses the work.

### The milestone, reached the same day

`test_a_claimed_state_answers_for_a_file_it_can_no_longer_read`.

The creator evaluates a file over RPC, deletes it, detaches and stops
pinging. The sweeper reaps the connection. A successor claims the
token, asks the same state for the same path, and gets 42 - from a
file that has not existed since before the sweep. A FRESH state on
the same server, asked in the same moment, says

    SysError: error: opening file '.../answer.nix':
    No such file or directory

That is `CLAUDE.md`'s milestone sentence, executable.

Two things it does NOT prove, and the first is the same caveat the
sibling test above carries. A handle id is the access capability, so
this shows the OBJECT survived, not that the CLAIM is what kept it -
`tasks/031`. And "does no re-evaluation" is shown by the file being
gone, not by counting evaluations: a cache hit that somehow
re-evaluated from a parsed expression it had kept would pass this.
Both are worth a better instrument, neither is worth a weaker claim.

One thing measured on the way. `wrapper_error` does not see this
failure: a declared Nix error crosses as ITSELF (`tasks/066`), so the
cold state's SysError went straight past a helper that catches only
`InternalError`. Written the wrong way first, and the traceback is
what said so.

### What is still missing

Watched files and background eager evaluation are still untouched.
`resetFileCache` is deliberately NOT bound: nothing but a test would
call it today, and 070 is the standing example of a binding with no
user. It becomes real surface when inotify invalidation needs it.

## 2026-09-03: libexpr will not say which files it read

The vision says the server "watches every non-Store file an
evaluation touched". Measured against the source before designing
anything, and the answer is that libexpr offers no way to ask.

`EvalState` knows. Three private members hold it:

    importResolutionCache   /foo        -> /foo/default.nix
    fileEvalCache           resolved    -> Value *
    positionToDocComment    per file

All three are under `private:` in the 2.34.8 header this build links
(`eval.hh:460`), so a binding cannot read them.

Nor can one be wrapped on the way in. `rootFS` is the accessor every
file read goes through, and it is a `const ref<SourceAccessor>` built
INSIDE the constructor from the settings alone (`eval.cc:267`) - the
constructor takes a lookup path, a store, two settings objects and an
optional build store, and no accessor. There is nothing to substitute.

Upstream is moving away from this, not toward it. In the 2.36pre
header also in this store, `rootFS` has moved under `private:` too
(`eval.hh:393, 424`), and `fileEvalCache` is still private.

### So the choice is not "how to watch" but "what can be watched"

- **What OUR boundary sees.** Every path handed to `eval_file`. Honest
  and free, and shallow: a file reached by `import` from inside a Nix
  expression is invisible, and that is most of a real evaluation.
- **A patched libexpr.** A friend accessor, an upstream PR, or a
  wrapping `SourceAccessor` the constructor accepts. Real coverage,
  and a dependency on a change this repo does not control.
- **The directory.** Watch the tree each evaluated file sits under,
  and accept over-watching. No API needed, and it invalidates on
  files no evaluation ever read.

This is Carl's call. The measurement is here so it is made against
what libexpr does rather than against what it might.

### Carl chose the second, and it needed no patch

"If we can create a friend class or something in our bindings only it
would be best, if we need to patch we can patch."

We can. [temp.spec]/6 says access checking is NOT performed on the
names used in an explicit instantiation, so a template taking the
member pointer as a non-type parameter may be instantiated with a
private one, and the friend it defines hands it out afterwards. Legal
and portable, and it reaches `fileEvalCache` from our own header with
nixpkgs untouched.

Compiled against the packaged 2.34.8 headers before anything was
written into the repo. The first attempt failed for a reason worth
keeping: `eval.hh` only FORWARD-declares
`boost::concurrent_flat_map`, so the member was reachable and its
type incomplete - `invalid use of incomplete type`. One include fixed
it, and the error proved the access half had already worked.

`huggorm::cached_files` is in `cpp/eval.hpp`, and it is a HELPER by
this repo's own test: generated code CALLS it, and the fact it
carries - a foreign library's private state - is one no declaration
can express. `EvalState.cached_files()` is the declaration; the
emitter writes the binding.

It fails LOUDLY if upstream renames or removes the member: the
explicit instantiation stops compiling. That is the right failure for
a reach into a private, and better than a silent empty answer.

**`cpp/eval.hpp` went 238 -> 257 code lines**, by the build's own
count. Recorded because CLAUDE.md says that number is not a budget to
spend, and because this file is the one that grew 108 -> 417 a
reasonable line at a time. Nineteen lines, and the argument for them
is written above them in the file.

### The gate

`test_a_file_reached_by_import_is_in_the_cache_too`, on all three
surfaces. A file that imports another is evaluated, and BOTH appear.

The inner one is the assertion that matters. Our own boundary sees
one path - the one handed to `eval_file` - and an evaluation reads
many, because `import` goes through `evalFile` too. That difference
is the whole reason this reads libexpr's cache rather than counting
what we were asked to evaluate.

Both are asserted absent BEFORE the evaluation, so a state answering
with a constant, or with every file it had ever seen, fails here
rather than passing by accident.

Seen to FAIL, by removing the `import` from the outer file:

    assert '.../inner.nix' in ['«nix-internal»/derivation-internal.nix',
                               '.../outer.nix']

So the discriminating assertion discriminates, and the failure shows
what the cache actually holds - including one entry that is not a
file at all. libexpr evaluates its own `derivation-internal.nix` out
of an in-memory accessor, and it renders as
`«nix-internal»/derivation-internal.nix`. A watcher has to skip what
it cannot stat; the declaration says so.

### What this still does not see

`builtins.readFile` and `builtins.path` do not go through
`fileEvalCache`. A watcher built on this watches every Nix file an
evaluation imported and no data file it read. The declaration says
so, which is the difference between a known limit and a wrong answer.

### What is next for the watcher: invalidation

Carl's goal for `cached_files` in his own words: "to be able to have a
live-reloading evaluation server (at some point), resetting the entire
eval cache database is not what we want to do at all."

`resetFileCache()` is public and is not a coarser version of the same
operation. Its body clears four things (`eval.cc:1155`):

    importResolutionCache->clear();   path -> resolved path
    fileEvalCache->clear();           the warm evaluations
    inputCache->clear();              fetched flake inputs
    positions.clear();                the PosTable

`inputCache` holds FETCHED inputs. For a state meant to live for days,
that is re-downloading over the network because one local file
changed. And it destroys the milestone this task is measured by: after
it, every input counts as changed because one was.

### The measurement the design rests on

Taken 2026-09-03, before asking for any C++. A cached importer does
not notice its import changing, and the cache holds no edge:

    outer.nix = `import inner.nix`   ->  42
    (inner.nix edited to `1 + 1`)
    cached outer = 42        a fresh state = 2
    cached_files = [«nix-internal»/derivation-internal.nix,
                    outer.nix, inner.nix]

Both files are listed and nothing says one imported the other. So
erasing `inner` alone would leave `outer` answering 42 for the life of
the state - SILENTLY, which is worse than over-clearing.

Per-path erase is therefore the right mechanism and is not sufficient
by itself. The edges are recoverable without more C++: `cached_files`
before and after one `eval_file(X)` differ by exactly the files that
evaluation read, which is X's closure. Forget the closure, not the
file.

### What the erase has to get right

`fileEvalCache` is keyed by the RESOLVED path. An erase of the path a
caller GAVE would leave `/foo/default.nix` cached after
`forget_file("/foo")`, and the next evaluation would re-resolve, hit
the stale value and answer from it. Both spellings go.

Safe because an `EvalState` is AFFINE in this repo - one thread per
state, which is huggorm's policy rather than libexpr's guarantee. The
map tolerates more; the reasoning should name the invariant that
actually holds.

Known limits to keep beside it: `positions` keeps entries for a
forgotten file (append-only metadata, harmless), and a file reached
only through `builtins.readFile` is invisible to all of this.

### Per-path invalidation, built

`EvalState.forget_file(path)` is declared, and `huggorm::forget_file`
is the helper it calls. Approved by Carl in advance, as CLAUDE.md
requires.

Two keys, because the two caches are keyed differently:

    importResolutionCache   given    -> resolved
    fileEvalCache           resolved -> Value *

So forgetting `/foo` erases `/foo/default.nix` as well as `/foo`.
Erasing only what the caller SAID would leave the value cached, and
the next evaluation would re-resolve, hit it and answer stale.

**A delta from the plan, named rather than slipped in.** The plan said
erase from `fileEvalCache`. The code also erases the matching
`importResolutionCache` entries. The reason is that a resolution goes
stale too - a symlink retargets, or a `/foo` gains or loses a
`default.nix` - and re-resolving costs one stat. The justification is
in the comment above those four lines, so a later reader is not left
guessing whether they were meant.

Collect before erase. `cvisit_all` holds a lock for the length of the
visit, so an erase from inside the visitor deadlocks on the same map.

Safe because an `EvalState` is AFFINE in this repo - one thread per
state at a time. That is huggorm's policy, not libexpr's guarantee:
the map tolerates concurrent writers, but nothing here defends the
read-then-erase against a racing evaluation refilling the entry.

**`cpp/eval.hpp` went 257 -> 277 code lines**, by the build's own
count, plus a 3-line `Cxx` body in the declaration. Recorded because
CLAUDE.md says the number is not a budget to spend.

Both APIs read from the packaged headers before anything was written,
and a compile probe against them ran first: `concurrent_flat_map::
erase(key_type const &)` at `concurrent_flat_map.hpp:799`, and
`SourcePath::operator==` at `source-path.hh:106`.

### The gate, and its own negative control

`test_forgetting_a_closure_picks_up_an_edited_import`, on all three
surfaces. Evaluate an importer, edit its import, forget the closure,
and the re-evaluation answers the NEW value.

The closure is the `cached_files` diff around one `eval_file`. That
helper lives in the TEST, not the binding, because it is a policy and
not a fact: two concurrent evaluations on one state would mix their
closures, and the affine state is what makes it hold.

Seen to FAIL, by making the `fileEvalCache` erase a no-op:

    assert 42 == 2
    FAILED test_forgetting_a_closure_picks_up_an_edited_import[sync]
    FAILED test_forgetting_a_closure_picks_up_an_edited_import[async]
    FAILED test_forgetting_a_closure_picks_up_an_edited_import[rpc]

That failure proves the fragile joint, which is not the erase.
`cached_files` renders a path with `to_string()` and `forget_file`
re-enters through `rootPath()`; if those two did not produce equal
`SourcePath`s, every erase would silently miss and the test would show
exactly this. It does not.

`test_forgetting_only_the_edited_file_leaves_the_importer_stale` is
the measurement above, kept as a test. It forgets `inner` alone and
asserts the importer still answers 42. Asserted rather than noted, so
a `forget_file` that grew a recursive erase would fail here and be
seen. It is not a wish that the answer stays stale - it is the
statement that per-file erase ALONE is not invalidation.

277 passed, from 271.

### What is left for the server

A watcher. Nothing here calls `inotify`, and nothing decides WHEN to
forget - a caller must notice the change itself and hand the closure
back. The pieces the watcher needs now exist: `cached_files` says what
to watch, the diff around `eval_file` says what belongs to what, and
`forget_file` drops one without dropping the rest.

Background eager evaluation is still untouched.

Known limits, unchanged: `positions` keeps entries for a forgotten
file (append-only metadata, harmless), and a file reached only through
`builtins.readFile` is invisible to all of this.
