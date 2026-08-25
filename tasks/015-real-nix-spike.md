# Real-Nix binding spike

Swap the mock for a first real type: bind genuine nix::StorePath from
the fetched Nix source (store path 2ijv0g6069dsh55z3bdr5ln2iv69mw7r),
keeping the mock alongside. Surfaces the last unknowns: real build
linkage, header quirks, namespace depth, boehmgc linkage against the
real library.

Gate: the review's verdict cluster (001/002/006) should be closed
first - runner lifecycle flaws convert from leaks to native crashes
against real libstore.

## Update 2026-08-25

The verdict cluster is closed: 001, 002 and 006 are all done, and 002's
escrow lease leak - found 2026-08-25 - is fixed with an invariant
check behind it.

Two NEW gates matter more than that cluster did, both blocking in
practice rather than in principle:

- 019 (typed Acquire). Real stores come from openStore(uri). An
  argument-less Acquire can construct nothing worth constructing
  against real libstore.
- 020 (binding-to-C++ link). The generator maps pxd types to Python
  ones by stripping a leading "C". Real headers will not cooperate.

021 (free functions) is not blocking but is where a lot of the real
libstore surface lives.

## Findings from reading the real source (2026-08-25)

Read while verifying 017's claim about StorePath. Store path
2ijv0g6069dsh55z3bdr5ln2iv69mw7r.

- `nix::StorePath() = delete`. Real Nix FORBIDS default construction.
  Our mock declares `StorePath() = default` "as binding glue", and
  store.pyx depends on it: every produced value does
  `StorePath.__new__(StorePath)` and then assigns `_ptr`. That pattern
  does not survive contact with libstore. The binding will need a
  different produced-value shape - a pointer that starts null and is
  set by a factory, or construction in place.
- StorePath holds one std::string. Its accessors return string_view
  INTO that member, so a Cython binding must copy before the owner
  dies, and must not hand a dangling view to Python.
- StorePath already has nlohmann JSON serialization upstream
  (adl_serializer, json_avoids_null). The `_wire = "value"` policy
  matches a category Nix already has, rather than inventing one -
  and there may be a real serializer to reuse instead of _parts().
- DerivedPath::to_string takes a `const StoreDirConfig &`. Wire-value
  serialization is not always a pure method of the value; some of it
  needs store context. _wire_fields assumes a self-contained
  _parts(); that assumption breaks here.
- The header layout is src/libstore/include/nix/store/*.hh, so the
  pxd include paths are "nix/store/path.hh" and friends.

## Starting 2026-08-25

Carl: "At this point it feels like we're implementing quite a lot of
Nix. Maybe it's better if we abort 034 and start linking against real
Nix?" Building function values into the mock was the point where mock
fidelity stopped paying.

Two things landed first, both because they make this step safer rather
than because they are part of it: the Python layers are now
cythonix-bindings / cythonix-generated / cythonix, and the suites are
73 pytest tests under anyio instead of three scripts with one main().
Against real Nix a failure can be a native crash, and a subprocess
server plus per-test isolation is what keeps one from taking the run.

### What the packaged headers give us

nixpkgs `nix` is 2.34.8 with a `dev` output. Everything this file
predicted from the source checkout holds against the packaged headers:

- `nix::StorePath() = delete`, a single `std::string baseName` member,
  and `to_string()` returning a `std::string_view` INTO it;
- headers at `include/nix/store/path.hh`, so the pxd include is
  `"nix/store/path.hh"`;
- pkg-config files for every library: nix-store.pc, nix-expr.pc,
  nix-util.pc and the C-api ones. `nix-store.pc` carries
  `-std=c++23`, `-lnixstore`, and a `Requires: nix-util,
  nlohmann_json >= 3.9`.

So the build side is pkg-config, not hand-rolled include paths. That
is a real difference from the mock, where setup.py takes one
FAKE_LIBRARY prefix and joins `include` and `lib` onto it.

### There is also a C API

`nix_api_store.h`, `nix_api_value.h`, `nix_api_expr.h` and friends
ship in the same dev output, with their own pkg-config files. That is
a second possible binding target and it is NOT obviously the wrong
one: a stable C ABI avoids every C++-in-Cython problem this repo has
worked around (`except +`, template types the pxd parser cannot
render, `StorePath() = delete` versus `__new__`-then-assign).

It is also a different project. The whole design here rests on reading
C++ declarations out of a pxd, and the C API would make the generator
read a C header instead. Worth a decision before the first binding is
written, not after.

**Decided 2026-08-25.** Carl: "The C API is not feature complete at
all which is why we're binding C++, we are doing 100% C++."

So the C API is not a fallback for the awkward cases either. Every
C++-in-Cython problem gets solved rather than routed around:
`StorePath() = delete` needs a produced-value shape that does not
default-construct, `except +` stays, and a template type in a pxd
renders as itself and maps to nothing until something maps it.

## Done 2026-08-25: nix::StorePath is bound

It parses, validates, and crosses the wire. `StorePath(
"7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1")` gives the base name,
the name part and the 32-character hash; `"not-a-store-path"` raises
with libstore's own message. The whole generator chain ran over it
unchanged: a manifest entry, a `StorePathMsg` wire message, a typed
constructor, and PEP 561 stubs carrying its docstrings.

### The predicted problems, and which were real

- **`StorePath() = delete` breaks the produced-value pattern.** NOT
  real. The pattern is `__new__` then assign a POINTER; nothing ever
  default-constructs the C++ object. Declaring a default constructor
  in the pxd would have been the mistake, and not declaring one costs
  nothing.
- **Accessors return views into a member.** Real, and handled: one
  `_view()` helper copies, and nothing that leaves the binding is a
  view. A view outliving its owner is a dangling pointer, not an
  exception, so there is no second chance to notice.
- **The pxd parser could not render the declarations.** NOT real.
  `string_view`, the `"hashPart"` C-name rename and the copy
  constructor all parse; the parser even drops the copy constructor
  from the constructor list on its own. One line was needed:
  `string_view` maps to `str`, because a binding copies.

### What actually bit

`__cinit__` runs on EVERY `__new__`, including the argument-less one a
copy needs, so it cannot also be where construction happens. The real
constructor lives in `__init__`, and a `_get()` guard raises rather
than dereferencing NULL for an object that was `__new__`ed and never
initialised. The mock never hit this because its `__init__` refuses:
it is produced, never constructed.

### Linking

Against the SPLIT components, not the `nix` package (Carl,
2026-08-25): `nix.libs.nix-{util,store,expr,fetchers,flake}`. `nix`
itself is the CLI and drags its closure - 158.6 MiB against 143.1 MiB
for nix-store alone, and only the libraries are ever loaded.

setup.py reads pkg-config rather than joining `include` and `lib` onto
a prefix. That is not a style choice: nix-store.pc carries
`-std=c++23`, a `Requires` chain into nix-util and nlohmann_json, and
a private link line nobody should reconstruct by hand. rpath comes
from the same place, because Nix has no global library path to fall
back on.

### Naming

Carl: "Let's not prefix everything with Nix when we're just getting
started, naming is important and a PITA to change later."

So the REAL type takes the real name and the mock yields it. Each mock
class gets its `Mock` prefix exactly when its real counterpart lands,
and the prefix disappears when the mock does. `StorePath` is real now;
`MockStorePath` is the mock's, and `CMockStorePath` its pxd alias.

Modules mirror Nix's own header layout rather than inventing one:
`nix/store/path.hh` is bound by `path.pyx` and declared by
`c_path.pxd`. When the mock leaves, `store.pyx` and `eval.pyx` are
free for `nix/store/store-api.hh` and `nix/expr/eval.hh`.

### Two things to decide before the next type

- **A nix::Error arrives in Python as RuntimeError.** Cython's
  `except +` maps anything that is not a std exception it knows onto
  RuntimeError, so `BadStorePath` loses its type on the way. The typed
  error path this repo already has (WrapperError.to_dict) wants a
  `except +translate_nix_error` handler instead, which is the
  documented Cython hook for exactly this.
- **The message carries ANSI escapes.** libstore formats errors with
  colour, so the RuntimeError string holds `\x1b[31;1merror:\x1b[0m`.
  That is wrong in a Python traceback and wrong over the wire. Nix has
  a setting for it; find it before the errors start mattering.
