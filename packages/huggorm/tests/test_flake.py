"""Locking and calling a flake (`flake.cc`), hermetically.

Every flake here is a `path:` flake in a temporary directory, and the
store is a chroot store beside it: `lockFlake` copies the source in,
and nothing reaches the network. The global registry is off, because
resolving any reference consults every registry layer and the default
global one is a URL.
"""

import json
import pathlib

import pytest

from huggorm_bindings import EvalState, Store, parse_flake_ref
from huggorm_bindings.errors import UsageError

pytestmark = pytest.mark.usefixtures("flakes")

NO_GLOBAL = {"flake-registry": ""}


def _flake(where: pathlib.Path, text: str) -> pathlib.Path:
    where.mkdir(parents=True)
    (where / "flake.nix").write_text(text)
    return where


@pytest.fixture
def state(tmp_path: pathlib.Path) -> EvalState:
    return EvalState(Store(str(tmp_path / "root")), NO_GLOBAL)


@pytest.fixture
def probe(tmp_path: pathlib.Path) -> pathlib.Path:
    dep = _flake(tmp_path / "src" / "dep", "{ outputs = _: { y = 1; }; }")
    return _flake(tmp_path / "src" / "probe", f"""{{
  description = "probe";
  inputs.dep.url = "path:{dep}";
  outputs = {{ dep, ... }}: {{ x = 7; y = dep.y; }};
}}""")


def test_a_locked_flake_calls_to_its_outputs(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    assert locked.description() == "probe"
    outputs = state.call_flake(locked)
    state.force(outputs)
    x, y = outputs.get("x"), outputs.get("y")
    state.force(x)
    state.force(y)
    assert (x.integer(), y.integer()) == (7, 1)


def test_the_lock_file_is_written_and_names_the_input(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    written = json.loads((probe / "flake.lock").read_text())
    assert written["nodes"]["root"]["inputs"] == {"dep": "dep"}
    dep = locked.find_input(["dep"])
    assert dep is not None
    assert dep.is_flake() is True
    assert dep.original_ref().to_attrs() == {
        "type": "path", "path": str(probe.parent / "dep")}
    assert dep.locked_ref().to_attrs()["type"] == "path"


def test_a_lock_that_is_not_written_leaves_no_file(
        state: EvalState, probe: pathlib.Path) -> None:
    state.lock_flake(parse_flake_ref(f"path:{probe}"), write_lock_file=False)
    assert not (probe / "flake.lock").exists()


def test_an_input_that_is_not_there_is_none(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    assert locked.find_input(["nope"]) is None
    assert locked.find_input([]) is None


def test_an_empty_input_path_is_refused(
        state: EvalState, probe: pathlib.Path) -> None:
    with pytest.raises(UsageError, match="must not be empty"):
        state.lock_flake(parse_flake_ref(f"path:{probe}"), update=[""])


def test_the_metadata_is_what_nix_flake_metadata_prints(
        state: EvalState, probe: pathlib.Path) -> None:
    locked = state.lock_flake(parse_flake_ref(f"path:{probe}"))
    metadata = json.loads(state.flake_metadata_json(locked))
    assert metadata["description"] == "probe"
    assert metadata["originalUrl"] == f"path:{probe}"
    assert metadata["path"].startswith("/nix/store/")
    assert set(metadata["locks"]["nodes"]) == {"root", "dep"}


def test_get_flake_answers_the_resolved_reference(
        state: EvalState, probe: pathlib.Path) -> None:
    resolved = state.get_flake(parse_flake_ref(f"path:{probe}"),
                               use_registries=False)
    assert resolved.to_attrs() == {"type": "path", "path": str(probe)}


def test_a_flake_setting_that_is_not_one_is_refused(
        state: EvalState, probe: pathlib.Path) -> None:
    with pytest.raises(UsageError, match="not a setting this call takes"):
        state.lock_flake(parse_flake_ref(f"path:{probe}"),
                         settings={"pure-eval": "true"})
