# Sum types on the wire

**DECIDED, IN PROGRESS.** The design below is settled; the arc that
carries it is being built. `tasks/058` deferred this; the evidence
that decided it is recorded here.

Asked by Carl: *"Nix daemon serializes these over the wire, i assume
for simplicity. If we can do a gRPC compatible version it'd be really
nice."*

## The type that forces the question

    struct DerivedPath : std::variant<DerivedPathOpaque, DerivedPathBuilt>
    struct DerivedPathBuilt { ref<const SingleDerivedPath> drvPath;
                              OutputsSpec outputs; };
    struct SingleDerivedPath : std::variant<Opaque, SingleDerivedPathBuilt>
    struct SingleDerivedPathBuilt { ref<const SingleDerivedPath> drvPath;
                                    OutputName output; };
    struct OutputsSpec : std::variant<All, Names>

A sum type, holding a reference to a RECURSIVE sum type, holding a
third sum type.

It matters because `DerivedPath` is the argument of `buildPaths`,
`queryMissing` and `buildPathsWithResults`. It is the gateway to
BUILDING, and `decl/store.py` binds none of those yet.

## Upstream has two serialisations and neither is protobuf-shaped

**The daemon uses ONE STRING.** `worker-protocol.cc:194`:

    conn.to << req.to_string_legacy(store);          // write
    return DerivedPath::parseLegacy(store, s);       // read

Carl's guess was right, and the cost is visible in that same
function. It needs a `StoreDirConfig &`, because the string is a full
`/nix/store/...-foo.drv!out`. Below protocol 1.30 it degrades to
`StorePathWithOutputs` and throws on two shapes it cannot express -
one of them with the message "protocols do not support that. Try
upgrading the Nix on the other end of this connection".

So the daemon's encoding is not a model to copy. It is a constraint
upstream lives with, and it has already cost a version gate and two
error cases.

**The JSON form is structured, and tagged by SHAPE rather than by a
tag** (`derived-path.cc:279-305`):

    SingleDerivedPath / DerivedPath  a STRING if Opaque, an OBJECT if Built
    DerivedPath::Built    {"drvPath": <SingleDerivedPath>, "outputs": <OutputsSpec>}
    SingleDerivedPath::Built  {"drvPath": <SingleDerivedPath>, "output": "out"}
    OutputsSpec           ["*"] if All, else a list of names

Two sentinels doing a tag's job: "is this JSON a string" and the magic
`["*"]`. Both work because the alternatives happen not to collide -
`*` is not a valid output name - which is a property to rely on, not a
design.

## gRPC is a better fit than either, and that is the finding

protobuf has a real tagged union and native message recursion, so
neither sentinel is needed and nothing is version-gated:

    message DerivedPathMsg {
      oneof raw { StorePathMsg opaque = 1; DerivedPathBuiltMsg built = 2; }
    }
    message SingleDerivedPathMsg {
      oneof raw { StorePathMsg opaque = 1; SingleDerivedPathBuiltMsg built = 2; }
    }
    message SingleDerivedPathBuiltMsg {
      SingleDerivedPathMsg drv_path = 1;      // recursive
      string output = 2;
    }
    message OutputsSpecMsg {
      oneof raw { bool all = 1; OutputNamesMsg names = 2; }
    }

**The transport is not the problem. The DECLARATION LANGUAGE is.**
`_wire_fields` is a flat product - `((name, type), ...)` - and
`_from_parts(*parts)` takes one value per field, positionally. Neither
can say "one of these".

## Decided

Reviewed by cython-reviewer. Answers in the order that matters, which
is not the order they were asked.

**Not "not yet" - "not alone".** The anti-pattern is a mechanism
landing with only round-trip tests as its consumer. The fix is not
deferral, it is SCOPING: one arc whose end is a store method taking a
DerivedPath against a real store. `buildPaths` cannot be bound without
DerivedPath, so the two were never separable - the mistake would be
shipping the mechanism standalone and binding the methods "later".

And there is a better first consumer than `buildPaths`:
**`queryMissing`**. Same argument type, READ-ONLY, and it answers
meaningfully against a hermetic chroot store - `willBuild`,
`willSubstitute`, sizes - where `buildPaths` needs a buildable
derivation the hermetic suite cannot yet make. So: mechanism ->
DerivedPath -> `query_missing` (hermetic, in the gate) -> `build_paths`
(live suite).

**There is no DerivedPathOpaque.** Upstream's Opaque is a one-member
struct around a StorePath; on the Python surface the opaque arm IS a
StorePath.

    DerivedPath = StorePath | DerivedPathBuilt

The proto sketch above already said so - `oneof raw { StorePathMsg
opaque = 1; ... }`. A caller writes `store.query_missing([sp,
DerivedPathBuilt(drv, outs)])` and never learns a wrapper class
existed. One fewer declaration, and isinstance dispatch over types
callers already hold.

**The union is a module-level ALIAS, and the alias name is the wire
name.** `DerivedPath = StorePath | DerivedPathBuilt` is real Python,
so import-then-transform resolves it and the reader records it. The
manifest carries a `unions` table beside `enums` - {alias: [arms]} -
and everything downstream mirrors the enum seam that already exists:
the schema emits one oneof message per alias, encode dispatches by
isinstance, decode by `WhichOneof`. An existing groove rather than a
new cut.

**The widened rule, stated whole.** An annotation union is either
`T | None` - presence, as today - or an ALIAS naming declared
wire-value classes and at most one bound value type such as StorePath.
Not scalars: `str | int` has no distinguishable arms. Not proxies: a
oneof arm granting leases is 031's bulk-lease problem. Not anonymous
unions in a signature: the alias is the name the wire needs. Not
`Alias | None` yet: refused until something needs it, with its own
reason.

**Recursion is bounded at the CODEC, with one constant.** The wire is
a trust boundary and the repo already bounds recursive structures
there. A deep chain from a peer is a RecursionError in `wire.py`,
which reaches a caller as an anonymous InternalError. One constant, one
check in the union decode path, one test, and a refusal that names the
limit. Declarations know nothing about it: depth is a fact about
CROSSING, not about the type.

**Rendering: `store.print_derived_path` / `store.parse_derived_path`,
and no `__str__`.** But the type is not unprintable - the value
machinery still owes it a parts-based `__repr__`, which is what a
debugger and a failing test need. What it lacks is the STORE-RELATIVE
rendering, and that needs a store by upstream's own signature. The
same sentence 040/042 already argued.

**Two types, following Nix.** `SingleDerivedPath` and `DerivedPath`
are semantically different - a reference to one output, versus a
request for a set - and the recursive arm needs SingleDerivedPath
anyway. With no Opaque classes the count is two Built classes and two
aliases, not four near-identical declarations.

**All is NOT None.** 041 gave `None` a meaning for a container -
absent IS empty - and "no outputs" and "all outputs" are opposite
requests. The magic `["*"]` refused from the JSON encoding must not
come back as a magic Python value either.

## The questions as they were asked

**1. How does a declaration SAY a sum type?**

(a) Two declared classes and a union annotation - `-> "Opaque |
Built"`. Reads as Python and `isinstance` dispatch is what a caller
would write. But `T | None` is the only union the reader handles
today, and it has been narrow on purpose.

(b) One class whose accessors are the arms, exactly one non-None.
Keeps `_wire_fields` a product, and makes an invalid state
representable in a way the C++ variant does not.

(c) `@tree`, which exists and is what `Value` uses. But a Value is a
PROXY walked by the RPC layer, not a value that crosses as its parts -
so this reuses a word for a second meaning.

**2. Recursion.** protobuf is fine with it. `wire.py`'s codec and a
hand-written `_from_parts` are the question, and so is whether a depth
limit is a real defence or an invented problem.

**3. Rendering needs a store, and this one looks answered.**
`DerivedPath::to_string` takes a `StoreDirConfig &`, so `str(dp)`
cannot be an accessor. The repo already has the shape -
`store.print_store_path(p)` and `store.parse_store_path(s)` - and
040/042 argue a StorePath is a NAME with rendering a separate act. So
`store.print_derived_path(dp)`, and no `__str__` at all.

**4. Two Python types or one?** `SingleDerivedPath` and `DerivedPath`
differ only in `output: str` versus `outputs: OutputsSpec`. Upstream
keeps both, and "follow Nix unless there is a reason not to" says two;
DRY twitches at four near-identical declarations.

**5. Should this wait?** The alternative is binding more of
nix::Store's flat surface first and letting the sum-type question wait
until something concrete needs it. A mechanism built before its user
is a mechanism shaped by a guess.
