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
SCALAR_NAMES = ("str", "int", "bool")

# Every Nix attribute name is a string, so a map key is always one.
# That is what makes an attribute set representable as a protobuf map
# at all (tasks/030).
MAP_KEY = "str"


def map_value(type_str: str) -> str | None:
    """The V of a `dict[str, V]` annotation, or None when this is not
    a map at all.

    Raises TypeError for a dict that cannot be one. A bare `dict` says
    nothing about its values, so it has no message to build. A
    non-string key has no protobuf spelling here. A map of maps needs
    a wrapper message, which proto3 does not synthesise."""
    node = ast.parse(type_str, mode="eval").body
    if isinstance(node, ast.Name) and node.id == "dict":
        raise TypeError(
            "bare 'dict' has no value type; declare dict[str, V] so the "
            "schema knows what the entries hold")
    if not (isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "dict"):
        return None
    args = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
    if len(args) != 2:
        raise TypeError(f"{type_str}: a map takes exactly two parameters")
    key, value = (ast.unparse(a) for a in args)
    if key != MAP_KEY:
        raise TypeError(
            f"{type_str}: a map key must be {MAP_KEY}, not {key}")
    if value == "dict" or value.startswith("dict["):
        raise TypeError(
            f"{type_str}: proto3 cannot nest a map inside a map. A nested "
            f"attribute set needs the recursive value message (tasks/030)")
    return value


def entry_name(field_name: str) -> str:
    """The synthesised MapEntry message for one map field, following
    protobuf's own convention: field `gc_counts` -> `GcCountsEntry`."""
    return "".join(part.title() for part in field_name.split("_")) + "Entry"
