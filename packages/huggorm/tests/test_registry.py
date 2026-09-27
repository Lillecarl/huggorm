"""The flake registry (`registry.cc`), without the network.

A write is checked by reading the JSON file it wrote. A listing is
checked in a fresh interpreter, because Nix caches every layer for the
life of the process and the first listing here would decide the rest.
"""

import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Iterator
from typing import Any, cast

import pytest
from test_path import _message

from huggorm.wire import WireCodec
from huggorm_bindings import (
    RegistryEntry,
    RegistryType,
    get_setting,
    registry_add,
    registry_remove,
    set_setting,
)
from huggorm_bindings.errors import UsageError

NO_GLOBAL = {"flake-registry": ""}


@pytest.fixture(autouse=True)
def flakes() -> Iterator[None]:
    """Nix parses no flake reference with the feature off, and neither
    does this. The setting belongs to the process, so it goes back."""
    before = get_setting("experimental-features") or ""
    set_setting("extra-experimental-features", "flakes")
    try:
        yield
    finally:
        set_setting("experimental-features", before)


def _flakes(file: pathlib.Path) -> list[dict[str, Any]]:
    flakes: list[dict[str, Any]] = json.loads(file.read_text())["flakes"]
    return flakes


def test_add_writes_the_entry_and_keeps_the_subdirectory(
        tmp_path: pathlib.Path) -> None:
    """The subdirectory is the `dir` extra attribute, not part of the
    target: `nix registry add` stores it that way."""
    file = tmp_path / "registry.json"
    wrote = registry_add(str(file), "mine", "github:o/r?dir=sub",
                         settings=NO_GLOBAL)
    assert (wrote.path(), wrote.removed(), wrote.target(), wrote.locked()) \
        == (str(file), 0, "github:o/r", None)
    assert _flakes(file) == [{
        "from": {"id": "mine", "type": "indirect"},
        "to": {"owner": "o", "repo": "r", "type": "github", "dir": "sub"},
    }]


def test_a_second_add_replaces_the_first(tmp_path: pathlib.Path) -> None:
    file = tmp_path / "registry.json"
    registry_add(str(file), "mine", "github:o/a", settings=NO_GLOBAL)
    wrote = registry_add(str(file), "mine", "github:o/b", settings=NO_GLOBAL)
    assert wrote.removed() == 1
    assert [f["to"]["repo"] for f in _flakes(file)] == ["b"]


def test_remove_reports_what_it_dropped(tmp_path: pathlib.Path) -> None:
    """Zero is an answer: Nix's own command says nothing either way."""
    file = tmp_path / "registry.json"
    registry_add(str(file), "mine", "github:o/a", settings=NO_GLOBAL)
    assert registry_remove(str(file), "other", settings=NO_GLOBAL).removed() == 0
    wrote = registry_remove(str(file), "mine", settings=NO_GLOBAL)
    assert (wrote.removed(), wrote.target()) == (1, None)
    # Nix's own write: a registry with no entry holds null, not [].
    assert json.loads(file.read_text())["flakes"] is None


def test_a_relative_path_parses_against_the_base(tmp_path: pathlib.Path) -> None:
    """Without a base directory a relative path does not parse at all."""
    (tmp_path / "flake").mkdir()
    file = tmp_path / "registry.json"
    wrote = registry_add(str(file), "mine", "path:./flake",
                         base=str(tmp_path), settings=NO_GLOBAL)
    assert wrote.target() == f"path:{tmp_path}/flake"


def test_a_name_that_is_not_a_fetcher_setting_is_refused(
        tmp_path: pathlib.Path) -> None:
    with pytest.raises(UsageError, match="not a fetcher setting"):
        registry_add(str(tmp_path / "r.json"), "a", "github:o/a",
                     settings={"pure-eval": "true"})


LIST = """
import json, sys
from huggorm_bindings import Store, load_config, registry_add, registry_entries
load_config()
registry_add(None, "user-one", "github:o/u")
registry_add(sys.argv[1], "global-one", "github:o/g?dir=sub",
             settings={"flake-registry": ""})
print(json.dumps([
    [e.layer(), e.source(), e.target(), e.extra_attrs(), e.exact()]
    for e in registry_entries(Store("dummy://"),
                              {"flake-registry": sys.argv[1]})]))
"""


def test_entries_come_from_every_layer_in_order(tmp_path: pathlib.Path) -> None:
    """The user layer first, then the global one: the order Nix
    consults them. A write with no path goes to the user's file."""
    env = {**os.environ, "NIX_CONFIG": "experimental-features = flakes",
           "XDG_CONFIG_HOME": str(tmp_path / "config")}
    out = subprocess.run(
        [sys.executable, "-c", LIST, str(tmp_path / "global.json")],
        env=env, capture_output=True, text=True, check=True)
    assert json.loads(out.stdout) == [
        ["user", "flake:user-one", "github:o/u", {}, False],
        ["global", "flake:global-one", "github:o/g", {"dir": "sub"}, False],
    ]


def test_an_attribute_keeps_its_arm_across_the_wire() -> None:
    """A bool is an int to isinstance, so a codec that tested arms in
    order would send True as the count 1."""
    from conftest import load_manifest

    manifest = load_manifest()
    codec = WireCodec()
    proto = {**manifest["wrappers"], **manifest["returned_types"]}["RegistryEntry"]
    attrs = {"text": "x", "count": 7, "yes": True, "no": False, "zero": 0}
    entry = cast("Any", RegistryEntry)._from_parts(
        RegistryType.GLOBAL, "flake:a", "github:o/a", attrs, True)
    msg = _message(proto["message"])()
    codec.value_to_msg("RegistryEntry", entry, msg)
    back = codec.value_from_msg("RegistryEntry", msg).extra_attrs()
    assert {k: (type(v), v) for k, v in back.items()} \
        == {k: (type(v), v) for k, v in attrs.items()}
