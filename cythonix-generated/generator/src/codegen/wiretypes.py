"""
How a declared type STRING is spelled, shared by the build and the wire.

Everything else about a type - value or proxy, which class, which
threading policy - comes out of the manifest, which got it from a
declaration next to the binding. This module holds the one part that
is not in the manifest, because it is not about the types at all: the
reading of an annotation. `dict[str, int]` is a map with int values
wherever it is written, and the schema builder and the runtime codec
have to agree on that.

It is copied into the generated package as `_wiretypes.py`, the same
way `_runtime.py` is, so the two sides read one definition instead of
two that drift.
"""

import ast

# The manifest's shape, bumped whenever a consumer that reads an OLD
# manifest would be wrong rather than merely missing something. The
# generator stamps it and every reader checks it: client and server are
# built together today, so they always agree with each other and would
# agree just as happily on yesterday's shape (tasks/022).
MANIFEST_SCHEMA = 1


def check_manifest(manifest: dict[str, object]) -> None:
    """Refuse a manifest this code cannot read."""
    found = manifest.get("schema")
    if found != MANIFEST_SCHEMA:
        raise ValueError(
            f"manifest schema {found!r}, expected {MANIFEST_SCHEMA}: it was "
            f"written by a different generator. Rebuild the package that "
            f"ships it against this one.")


# The types that go in a field as themselves. The schema maps them to
# proto types and the codec maps them to constructors; both start here.
#
# bytes is here because a store holds FILES. Their contents are not
# text and must not be encoded as if they were: a str field would
# round-trip a NAR into mojibake, and the hash that names the store
# path would be a hash of the wrong thing.
SCALAR_NAMES = ("str", "int", "bool", "bytes")

# Every Nix attribute name is a string, so a map key is always one.
# That is what makes an attribute set representable as a protobuf map
# at all (tasks/030).
MAP_KEY = "str"

# The containers a protobuf field can be. A map is `map<K, V>`, a list
# is a repeated field, and proto3 nests NEITHER inside the other: there
# is no repeated map field and no map of repeated values.
CONTAINERS = ("dict", "list")


# Modules an annotation may name directly. A declared type is normally
# a builtin or a binding class; `pathlib.Path` is neither. It is a
# dotted name whose head is a module, and an emitted module that
# annotates with it needs a plain `import pathlib`.
#
# These are NOT wire types, and that is the point. A method annotated
# with one gets no rpc - the schema builder says so, and the manifest
# records it - so this exists purely so the IN-PROCESS surface can
# still say what it returns. Store.real_path answers with a path on the
# machine the store runs on, which is a real method and not a remote
# call.
FOREIGN_MODULES = ("pathlib",)


def head(type_str: str) -> str | None:
    """The head of an annotation string.

    `dict` for `dict[str, int]`, `int` for `int`, None for anything
    that is neither a plain name nor a subscripted one. Reading the
    head is how a caller asks "what KIND of thing is this" without
    parsing the parameters it does not care about."""
    node = ast.parse(type_str, mode="eval").body
    if isinstance(node, ast.Subscript):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def names_in(type_str: str) -> set[str]:
    """Every type named inside one annotation string, subscripts
    included.

    `dict[str, Value]` names Value; reading the head alone says `dict`
    and misses it. That is what decides which imports an emitted module
    needs, and which types a contract check has to look at."""
    node = ast.parse(type_str, mode="eval").body
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _parameters(type_str: str, container: str) -> list[str] | None:
    """The parameters of `container[...]`, or None when `type_str` is
    something else entirely.

    Raises TypeError for the bare container. `dict` says nothing about
    what its entries hold and `list` says nothing about its elements,
    so neither has a field the schema can build."""
    node = ast.parse(type_str, mode="eval").body
    if isinstance(node, ast.Name) and node.id == container:
        raise TypeError(
            f"bare {container!r} has no element type; declare "
            f"{container}[...] so the schema knows what it holds")
    if not (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == container):
        return None
    args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    return [ast.unparse(a) for a in args]


def map_value(type_str: str) -> str | None:
    """The V of a `dict[str, V]` annotation, or None when this is not
    a map at all.

    Raises TypeError for a dict that cannot be one. A bare `dict` says
    nothing about its values, so it has no message to build. A
    non-string key has no protobuf spelling here. A map of containers
    needs a wrapper message, which proto3 does not synthesise."""
    args = _parameters(type_str, "dict")
    if args is None:
        return None
    if len(args) != 2:
        raise TypeError(f"{type_str}: a map takes exactly two parameters")
    key, value = args
    if key != MAP_KEY:
        raise TypeError(
            f"{type_str}: a map key must be {MAP_KEY}, not {key}")
    if (inner := head(value)) in CONTAINERS:
        raise TypeError(
            f"{type_str}: proto3 cannot put a {inner} inside a map. A nested "
            f"attribute set needs the recursive value message (tasks/030)")
    return value


def list_value(type_str: str) -> str | None:
    """The T of a `list[T]` annotation, or None when this is not a list
    at all.

    A list is a repeated field, which is why it has no entry message
    the way a map does. It shares the map's limit: proto3 gives a
    repeated field one element type, so a list OF a container has no
    spelling either."""
    args = _parameters(type_str, "list")
    if args is None:
        return None
    if len(args) != 1:
        raise TypeError(f"{type_str}: a list takes exactly one parameter")
    item = args[0]
    if (inner := head(item)) in CONTAINERS:
        raise TypeError(
            f"{type_str}: proto3 cannot repeat a {inner}. A list of them "
            f"needs the recursive value message (tasks/030)")
    return item


def entry_name(field_name: str) -> str:
    """The synthesised MapEntry message for one map field, following
    protobuf's own convention: field `gc_counts` -> `GcCountsEntry`."""
    return "".join(part.title() for part in field_name.split("_")) + "Entry"
