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
    """One declared parameter of a remote call.

    No default. The generated method resolved defaults before the call
    reached the runtime, so every argument a spec describes is
    present - carrying one here would suggest the runtime fills it in,
    and it never does."""

    name: str
    type: str


@dataclass(frozen=True, slots=True)
class Call:
    """One remote method, decided at build time.

    `path` is the gRPC route, `req` and `resp` name the two messages
    in the descriptor pool, `args` is the declared parameter list in
    order, and `returns` is the declared return type.

    Everything here is a constant. The client resolves nothing: it
    fills `req` from `args`, sends it to `path`, and reads `result`
    out of `resp` as `returns`."""

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
