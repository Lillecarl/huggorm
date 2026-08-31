# Most of this suite cannot run in the Nix sandbox

Carl, 2026-08-25: "Something that's going to become quite important
and obvious soon is that not much of this test suite can run inside
the Nix sandbox, it must run on the live system (in the future it can
run against chroot stores or in user namespaces with
local-overlay-store but we're not really there yet)."

## Where it stands

Every test runs in one place: `nix build --file . huggorm` runs the
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

## What is settled

Carl: "We need to start marking which tests can run in a sandbox and
not [...] and invoke pytest from a devshell to run tests outside of
the sandbox. Then we can progress with addToStore and others slowly
but safely."

**One marker, `live`, and it names the REQUIREMENT.** "This needs the
machine's own store", not "this runs in the devshell". A venue can
change; what a test needs cannot.

**The default is hermetic**, which is the part worth arguing. Marking
the live ones means a forgotten mark fails IN THE BUILD, loudly, and
someone fixes it the same afternoon. The other polarity fails by
silence: an unmarked test drops out of the build gate and nobody finds
out. Fail-loud beats fail-quiet even when it means marking the larger
half, and `pytestmark` at module scope makes a whole suite one line
when that day comes.

**A bare `pytest` runs everything.** That is what a devshell wants.
The build is the one that restricts, with `-m "not live"`. So the
narrower run is the one that has to ask.

**`nix run --file . test`** beside `check`, taking pytest arguments.
It puts the working tree's `huggorm` ahead of the installed copy, so
an edit is testable without a rebuild - `huggorm_bindings` and
`huggorm_generated` still come from the store, one being compiled and
the other generated.

The live suite has one inhabitant, and it is the test this whole
question came from: `query_all_valid_paths` on a store that HOLDS
something. Each element arrives as a heap pointer the binding owns, so
the loop hands each to a wrapper and blanks the slot. Skip the
blanking and the test fails on a freed StorePath. A sandbox sees an
empty list and none of it.

## Answered: a chroot store CAN hold something

That was the question worth asking, because the answer moves work back
INTO the sandbox rather than out of it.

`Store.add_to_store` writes to a chroot store rooted in `tmp_path`
with no daemon, no `/nix/var` and no network. So the store suite -
content addressing, the vocabulary libstore accepts, and the ownership
loop in `query_all_valid_paths` - is hermetic after all, and runs on
every build.

`addToStoreFromDump` is what made it small. `addToStore` takes a
`SourcePath`, which is a filesystem abstraction and a large surface;
the dump variant takes bytes, which is exactly what a test has.

The live suite keeps one test, and it is honest about what only it
covers: `auto` is the DAEMON - a different implementation on both
sides of a socket, holding tens of thousands of paths rather than
three.

## Still open

**What does a live test get to assume?** `Store("auto")` is whatever
the machine says. That makes a test easy and the machine a dependency.
Nothing is blocked on answering it while the live suite is one test.

The further version Carl names - `local-overlay-store` in a user
namespace - is what would let a sandbox hold a store with real
content in it, rather than three files this suite put there.
