"""What `nix eval` and `nix build` need from an evaluated value.

`Value.drv_path` joins evaluation to building: `DerivedPathBuilt`
needs the `.drv` a derivation names. `to_json` is `nix eval --json`,
and `base` is the directory a relative path names from.

The state runs against a chroot store, because reading `drvPath` and
copying a path both write to it.
"""

import pathlib
from typing import Any

import pytest

DRV = ('derivation { name = "joined"; system = "x86_64-linux"; '
       'builder = "/bin/sh"; }')


@pytest.fixture
def state(tmp_path: pathlib.Path) -> Any:
    from huggorm_bindings import EvalState

    return EvalState(str(tmp_path))


def test_a_derivation_names_its_drv(state: Any, tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import Store

    path = state.eval_expr(DRV).drv_path()
    assert path.is_derivation()
    assert path.name() == "joined.drv"
    assert Store(str(tmp_path)).is_valid_path(path), "reading it wrote it"


def test_the_drv_is_the_one_nix_computes(state: Any) -> None:
    """The same path `drvPath` spells, not one derived another way."""
    v = state.eval_expr(DRV)
    spelled = state.eval_expr(f"({DRV}).drvPath").string_value()
    assert spelled == f"/nix/store/{v.drv_path().to_string()}"


def test_an_attribute_set_that_is_not_a_derivation_refuses(
        state: Any) -> None:
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError, match="not a derivation"):
        state.eval_expr("{ a = 1; }").drv_path()


def test_it_joins_to_what_a_store_plans(
        state: Any, tmp_path: pathlib.Path) -> None:
    """The whole join: evaluate, name the `.drv`, ask the store what
    building it would take. Nothing here can build, so the answer is
    a plan and not a build."""
    from huggorm_bindings import DerivedPathBuilt, OutputsSpec, Store

    drv = state.eval_expr(DRV).drv_path()
    missing = Store(str(tmp_path)).query_missing(
        [DerivedPathBuilt(drv, OutputsSpec(all=True))])
    assert missing.will_build() == [drv]


def test_a_relative_path_names_from_the_base(
        state: Any, tmp_path: pathlib.Path) -> None:
    got = state.eval_expr("toString ./foo", str(tmp_path))
    assert got.string_value() == f"{tmp_path}/foo"


def test_without_a_base_it_names_from_the_working_directory(
        state: Any, monkeypatch: pytest.MonkeyPatch,
        tmp_path: pathlib.Path) -> None:
    """What `nix eval --expr` does."""
    monkeypatch.chdir(tmp_path)
    assert state.eval_expr("toString ./foo").string_value() == \
        f"{tmp_path}/foo"


def test_json_is_what_nix_eval_prints(state: Any) -> None:
    import json

    got = state.eval_expr('{ a = [ 1 2.5 "x" null true ]; b.c = 1 + 1; }')
    assert json.loads(got.to_json()) == {
        "a": [1, 2.5, "x", None, True], "b": {"c": 2}}


def test_a_path_stays_a_path_unless_copied(
        state: Any, tmp_path: pathlib.Path) -> None:
    (tmp_path / "f").write_text("hi\n")
    got = state.eval_expr("./f", str(tmp_path))
    assert got.to_json() == f'"{tmp_path}/f"'
    copied = got.to_json(copy_to_store=True)
    assert copied.startswith('"/nix/store/') and copied.endswith('-f"')


def test_a_function_has_no_json(state: Any) -> None:
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError, match="function"):
        state.eval_expr("{ f = x: x; }").to_json()


def test_a_string_with_nothing_to_build_realises_as_itself(
        state: Any) -> None:
    assert state.eval_expr('"plain"').realise_string() == "plain"


def test_a_realised_string_names_a_path_that_is_there(
        state: Any, tmp_path: pathlib.Path) -> None:
    """A path interpolated into a string is copied to the store, and
    the context names it. Realising it answers a path the state's
    store holds."""
    from huggorm_bindings import Store

    (tmp_path / "f").write_text("hi\n")
    got = state.eval_expr('"${./f}"', str(tmp_path)).realise_string()
    store = Store(str(tmp_path))
    assert store.is_valid_path(store.parse_store_path(got))


def test_an_argument_vector_realises_each_element(state: Any) -> None:
    got = state.eval_expr('[ "a" "b${toString 1}" ]').realise_argv()
    assert got == ["a", "b1"]


def test_an_unbuildable_context_raises(state: Any) -> None:
    """The derivation's builder does not exist, so the build fails,
    and the realise says so rather than answering a missing path."""
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError):
        state.eval_expr(f'"${{{DRV}}}"').realise_string()


def test_realising_is_not_an_import_from_derivation(
        tmp_path: pathlib.Path) -> None:
    """With IFD off, the realise still tries the build. It fails
    here, because the builder does not exist, and the failure must be
    the build's own, not the IFD refusal."""
    from huggorm_bindings import EvalState
    from huggorm_bindings.errors import NixError

    state = EvalState(str(tmp_path),
                      {"allow-import-from-derivation": "false"})
    with pytest.raises(NixError) as caught:
        state.eval_expr(f'"${{{DRV}}}"').realise_string()
    assert "allow-import-from-derivation" not in str(caught.value)
