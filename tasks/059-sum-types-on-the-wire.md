# Sum types on the wire

**OPEN.** No code. This is the decision `tasks/058` deferred, written
down with what upstream actually does so the next person argues from
evidence rather than from memory.

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

## The open questions

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
