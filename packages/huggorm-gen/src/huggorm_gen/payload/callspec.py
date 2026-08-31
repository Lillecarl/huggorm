"""What one remote call needs, as a typed value.

Copied into the emitted package as `_callspec.py`, the way
`wiretypes.py` and `runtime.py` are: the generated RPC classes build
these and the hand-written client reads them, so one definition is
copied rather than two kept in step.

## Why this is not a dict

It was one. Every generated method carried an entry out of
`self._rpc: dict[str, dict[str, Any]]`, which the manifest had
handed over as JSON. That worked and it proved nothing: `--strict`
cannot check a key that does not exist, a field spelled wrong, or a
`params` list whose entries are the wrong shape. The whole point of
generating the client was that a typechecker could see it, and the
one part a caller cannot see was the one part still untyped.

A frozen dataclass answers all three. The emitter builds one per
method, at import, and the checker reads every field.

## Why the types are still strings

`Arg.type` is `"list[StorePath]"`, not a class. The codec resolves a
declared type STRING against the wire policy - a scalar goes in as
itself, a wire-value decomposes into its parts, a proxy crosses as a
handle - and it does that by name because the name is what the
declaration wrote. Holding the class here would mean importing every
bound type into a module that only routes them.

The spelling is checked, just not here: `check_wire_contract` refuses
a type with no policy at build time, and `WireCodec.kind` raises on
one at run time.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Arg:
    """A declared name and its declared type.

    Used for both things that shape is: a parameter of a remote call,
    and one part of a wire value. They are not two ideas that happen
    to look alike - both say "this name carries a value of this
    declared type", and the codec treats them identically. Two
    dataclasses would be one fact stated twice.

    No default. The generated method resolved defaults before the call
    reached the runtime, so every argument a spec describes is
    present - carrying one here would suggest the runtime fills it in,
    and it never does."""

    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Call:
    """One remote method, decided at build time.

    `name` is the declared method, `path` is the gRPC route, `req` and
    `resp` name the two messages in the descriptor pool, `args` is the
    declared parameter list in order, and `returns` is the declared
    return type.

    Everything here is a constant. The client resolves nothing: it
    fills `req` from `args`, sends it to `path`, and reads `result`
    out of `resp` as `returns`. The server reads the same value the
    other way round, and `name` is the one field only it needs - the
    method to call on the object the handle resolved to.

    One dataclass for both sides, not two that agree. The two ends of
    one call cannot disagree about its shape if there is only one
    statement of it, and the build emits that statement once."""

    name: str
    path: str
    req: str
    resp: str
    args: tuple[Arg, ...]
    returns: str


@dataclass(frozen=True, slots=True)
class Acquire:
    """How to construct one class remotely.

    Separate from `Call` because a constructor has no handle to be
    called ON - it is what produces one - and answers a bare handle
    rather than a typed result. `cls` is the declared class name, which
    the client needs to build the right proxy around the answer.

    Not every constructible class has one. A class that crosses as a
    VALUE has no handle to construct into: a caller builds it locally
    and passes it as an argument."""

    cls: str
    path: str
    req: str
    args: tuple[Arg, ...]
    # How many of `args` a caller MUST pass. A constructor parameter
    # may carry a default, and the far side fills one in - so the
    # client checks the count rather than refusing every short call.
    #
    # A number, not a default VALUE. The value is already on the
    # binding's own __init__ and on the stub; repeating it here would
    # be a second place for it to be wrong, and nothing on this side
    # would ever apply it.
    required: int
    # The parameters whose declared default is the None LITERAL, by
    # name. A different fact from `required`, and conflating them
    # decoded `Store(uri="auto")` as an optional field and hit
    # "Field Store_AcquireReq.uri does not have presence".
    #
    # `required` is about ARITY: how many arguments a caller must
    # pass. This is about PRESENCE: whether the proto field can tell
    # unset from the zero value. A parameter with no default at all is
    # required and has no presence; one defaulting to "auto" is
    # optional to the caller and still has no presence; only one
    # defaulting to None has it.
    optional: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Walk:
    """How to read one CONTAINER of a value tree.

    A size accessor and the accessors that read a position. A list
    answers one value per index; an attribute set answers a name and a
    value, so `name` is empty for a list.

    Accessor NAMES, not bound methods. The walker holds the spec and
    the object separately - it is handed a fresh object per node -
    so it looks each one up on the node it is walking."""

    size: str
    value: str
    name: str = ""


@dataclass(frozen=True, slots=True)
class Tree:
    """How a value that HOLDS other values is walked.

    Declared next to the binding, because only the declaration knows
    which of a type's methods answers its kind and which reads a
    child. The server walks a whole tree in one hop, so it needs all
    of this before it starts.

    `identity` is what makes two nodes the same node, and it is
    declared rather than assumed: Python identity is not it wherever a
    binding builds a fresh wrapper per access. Empty falls back to
    `id()`.

    `scalars` maps a declared kind name to the pair (declared type,
    accessor). A kind absent from it is a thunk or something else the
    declaration does not name, and it crosses as a proxy."""

    kind: str
    identity: str
    scalars: dict[str, tuple[str, str]]
    list: Walk
    attrs: Walk
