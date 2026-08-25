# Evaluation server: persistent state, watched files, background eval

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
