"""What one remote call needs, as a typed value.

Copied into the emitted package as `_callspec.py`, the way
`_wiretypes.py` and `_runtime.py` are: the generated RPC classes build
these and the hand-written client reads them, so one definition is
copied rather than two kept in step.

## Why this is not a dict

A `dict[str, Any]` spec would work and prove nothing: `--strict`
cannot check a key that does not exist, a field spelled wrong, or a
`params` list whose entries are the wrong shape. The point of
generating the client is that a typechecker can see it, and a dict
leaves the call spec as the one untyped part.

A frozen dataclass answers all three. The emitter builds one per
method, at import, and the checker reads every field.

## Why a type is a `Wire`, not a class

`Arg.type` says how a value crosses - `Wire(WireKind.LIST,
item=Wire(WireKind.VALUE, "StorePath"))` - and names a class only by
its declared name. The build resolved every type once, so the codec
dispatches on `kind` and reads no annotation. Holding the class itself would mean importing every
bound type into a module that only routes them.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WireKind(StrEnum):
    """What the codec does with one `Wire`."""

    # One builtin field. `name` is `str`, `int`, `uint`, `float`,
    # `bool` or `bytes`, or a type that goes in one, such as
    # `datetime.timedelta`.
    SCALAR = "scalar"
    # A string vocabulary, which crosses as its str value.
    ENUM = "enum"
    # A message, rebuilt from its parts. `name` is the declared class
    # or alias.
    VALUE = "value"
    UNION = "union"
    ERROR = "error"
    # A handle. `name` is the class.
    PROXY = "proxy"
    # An object the client keeps, by the client's id for it. `name` is
    # the class, whose `CALLBACKS` the server calls (huggorm#153).
    CLIENT = "client"
    # A repeated field or a `map<string, V>`. `item` is what it holds.
    LIST = "list"
    MAP = "map"


@dataclass(frozen=True, slots=True)
class Wire:
    """How one declared type crosses, decided at build time.

    `optional` is presence: the field may be unset, and an unset one
    reads back as None."""

    kind: WireKind
    name: str = ""
    item: Wire | None = None
    optional: bool = False


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
    type: Wire


@dataclass(frozen=True, slots=True)
class Call:
    """One remote method, decided at build time.

    `name` is the declared method, `args` is the declared parameter list
    in order, and `returns` is the declared return type.

    Everything here is a constant. The client resolves nothing: it
    encodes the arguments by `args`, sends them under `index`, and
    decodes the answer as `returns`, which is None for a call that
    answers nothing. The server reads the same value the other way
    round, and `name` is the one field only it needs - the method to
    call on the object the handle resolved to.

    One dataclass for both sides, not two that agree. The two ends of
    one call cannot disagree about its shape if there is only one
    statement of it, and the build emits that statement once.

    `index` is the call's position in `CALLS`, and its number on the
    wire. Both ends run the same build, so a number need not survive
    a rebuild."""

    index: int
    name: str
    args: tuple[Arg, ...]
    returns: Wire | None


@dataclass(frozen=True, slots=True)
class Local:
    """One method only an in-process object runs.

    No `index` and no `args`: the wire cannot carry this call, and
    `refusal` says why. The runner reads `name` and `returns` as it
    reads a `Call`'s."""

    name: str
    returns: Wire | None
    refusal: str


@dataclass(frozen=True, slots=True)
class Hook:
    """One method Nix calls on an object a program implements.

    `posted` is `@posted`: Nix goes on before an async object's call has
    run (huggorm#155)."""

    call: Call
    posted: bool = False


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

    index: int
    cls: str
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


# How one node of a value tree is read. Accessor NAMES, not bound
# methods: the walker is handed a fresh object per node, so it looks
# each one up on the node it is walking.
@dataclass(frozen=True, slots=True)
class Leaf:
    """A node that crosses as one scalar. `wire` is its declared type,
    which picks the arm; `read` answers it."""

    wire: str
    read: str


@dataclass(frozen=True, slots=True)
class Null:
    """A node that crosses as None."""


@dataclass(frozen=True, slots=True)
class Items:
    """A node that holds values by position."""

    size: str
    item: str


@dataclass(frozen=True, slots=True)
class Entries:
    """A node that holds values by name."""

    size: str
    name: str
    value: str


@dataclass(frozen=True, slots=True)
class Tree:
    """How a value that HOLDS other values is walked.

    Declared next to the binding, because only the declaration knows
    which of a type's methods answers its kind and which reads a
    child. The server walks a whole tree in one hop, so it needs all
    of this before it starts.

    `kinds` maps each answer of the `kind` accessor to how that node
    is read. An answer it does not hold is a thunk or something else
    the declaration does not name, and it crosses as a proxy.

    `identity` is what makes two nodes the same node, and it is
    declared rather than assumed: Python identity is not it wherever a
    binding builds a fresh wrapper per access. Empty falls back to
    `id()`.

    `force` forces a node in place and `stop` keeps an `Entries` node a
    proxy, both only in a walk that forces. Empty means the type
    declares neither."""

    kind: str
    kinds: dict[str, Leaf | Items | Entries | Null]
    identity: str = ""
    force: str = ""
    stop: str = ""


@dataclass(frozen=True, slots=True)
class Builds:
    """How Python data becomes a value, by the methods of the type that
    makes values.

    `leaves` is keyed by a tree leaf's wire type. `add_item` and
    `add_entry` fill what `items` and `entries` made."""

    null: str
    leaves: dict[str, str]
    items: str
    add_item: str
    entries: str
    add_entry: str
