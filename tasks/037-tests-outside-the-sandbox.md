# Most of this suite cannot run in the Nix sandbox

Carl, 2026-08-25: "Something that's going to become quite important
and obvious soon is that not much of this test suite can run inside
the Nix sandbox, it must run on the live system (in the future it can
run against chroot stores or in user namespaces with
local-overlay-store but we're not really there yet)."

## Where it stands

Every test runs in one place: `nix build --file . cythonix` runs the
91 pytest tests inside the build sandbox. That was free while the mock
library backed everything, because a mock needs nothing from the
system.

Real Nix does. The sandbox gives a build no daemon, no
`/nix/var/nix/db`, and no way to write to the store it can see. So a
binding of a real store can only be tested on the half that needs
nothing:

- `dummy://` opens and refuses what it does not implement;
- a chroot store rooted in `tmp_path` opens and answers, but it is
  empty and stays empty.

`Store.query_all_valid_paths` is the first place this bites. It is
covered for the answer being a list, for the empty case, and for the
store that refuses the question. The loop that takes ownership of each
element never runs, because no store in the sandbox holds anything. By
hand against the ambient store it answers 23163 paths.

It gets worse from here rather than better. An evaluator needs a store
to substitute from; a fetcher needs the network; anything that builds
needs a daemon.

## What this is not

Not a reason to stop testing in the sandbox. What runs there today
runs fast, hermetically, and on every build - the codegen, the wire,
the lifecycle, the mock end to end. That half should stay exactly
where it is.

The question is where the OTHER half goes, and it needs an answer
before there is much of it.

## The shape of the answer

Two suites, split by what they need, and a way to run each:

- **hermetic** - everything that needs no system state. Stays in the
  build, stays a gate.
- **live** - everything that touches a real store. Runs against the
  live system, so it cannot be a `nix build` check: a build that reads
  `/nix/var` is not reproducible and Nix is right to forbid it.

Open questions, in the order they need answering:

1. **How does a live run happen?** `nix run --file . check` already
   exists for the fast gates and is not sandboxed. A `nix run --file
   . test-live` beside it is the obvious shape.
2. **What marks a test live?** A pytest marker is the cheap answer and
   it puts the decision next to the test. The hermetic run deselects
   it; the live run selects it.
3. **What does a live test get to assume?** A daemon, or only a store
   directory? Answering "a daemon" makes the tests easy and the
   machine a dependency.
4. **Can a chroot store be made to HOLD something?** That is the one
   that would move work back into the sandbox rather than out of it -
   a chroot store plus a binding that writes (`addToStore`, which
   needs `SourcePath`, `ContentAddressMethod` and `HashAlgorithm`
   first). Carl names `local-overlay-store` in a user namespace as the
   further version of this.

## Why not now

Nothing is blocked on it yet: the live half is one test's worth. It
becomes urgent at the first binding that must WRITE to a store, and
that is close.
