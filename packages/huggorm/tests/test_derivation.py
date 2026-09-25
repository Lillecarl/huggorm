"""From an evaluated derivation to something a store can build.

`Value.drv_path` is the join: evaluation answers an attribute set,
and `DerivedPathBuilt` needs the `.drv` it names. Reading `drvPath`
writes the `.drv`, so the state runs against a chroot store.
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
