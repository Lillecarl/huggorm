# Evaluation server: persistent state, watched files, background eval

**OPEN.** The lifetime contract landed; the service did not. No warm
cache, no inotify graph, no background evaluation.

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
