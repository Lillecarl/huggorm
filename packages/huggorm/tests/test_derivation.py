"""A `.drv`, read and written: `nix derivation show` and `add`.

The derivations are instantiated by evaluating them against a chroot
store, which writes the `.drv` and needs no builder.
"""

import importlib.resources
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

LEAF = ('derivation { name = "leaf"; system = "x86_64-linux"; '
        'builder = "/bin/sh"; args = [ "-c" "echo" ]; '
        'outputs = [ "out" "dev" ]; FOO = "bar"; }')
# Like LEAF, but with the builder `nix develop` accepts. Nothing
# builds here, so the path never has to exist.
BASH_LEAF = ('derivation { name = "leaf"; system = "x86_64-linux"; '
             'builder = "/bin/bash"; args = [ "-c" "echo" ]; '
             'outputs = [ "out" "dev" ]; FOO = "bar"; }')
ROOT = (f'derivation {{ name = "root"; system = "x86_64-linux"; '
        f'builder = "/bin/sh"; dep = ({LEAF}).dev; src = ./src; }}')
FIXED = ('derivation { name = "fixed"; system = "x86_64-linux"; '
         'builder = "/bin/sh"; outputHashMode = "flat"; '
         'outputHashAlgo = "sha256"; outputHash = '
         '"sha256-47DEQpj8HBSa+/TWmW+nGUhGKf1Kq8BQi/ljyeGd3sQ="; }')
# `outer` needs `out` of the derivation that building `inner` makes:
# the one shape that gives an input a child node.
INNER = ('derivation { name = "inner"; system = "x86_64-linux"; '
         'builder = "/bin/sh"; __contentAddressed = true; '
         'outputHashMode = "text"; outputHashAlgo = "sha256"; }')
OUTER = (f'derivation {{ name = "outer"; system = "x86_64-linux"; '
         f'builder = "/bin/sh"; '
         f'args = [ (builtins.outputOf ({INNER}).outPath "out") ]; }}')


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Any:
    from huggorm_bindings import Store

    return Store(str(tmp_path))


@pytest.fixture
def dynamic_derivations() -> Iterator[None]:
    """`builtins.outputOf` and what it needs, on for one test."""
    from huggorm_bindings import get_setting, set_setting

    before = get_setting("experimental-features") or ""
    set_setting("extra-experimental-features",
                "ca-derivations dynamic-derivations")
    try:
        yield
    finally:
        set_setting("experimental-features", before)


def instantiate(tmp_path: pathlib.Path, expr: str) -> Any:
    from huggorm_bindings import EvalState, Store

    (tmp_path / "src").write_text("source\n")
    return EvalState(Store(str(tmp_path))).eval_expr(expr, str(tmp_path)).drv_path()


def test_the_plain_fields_read_back(
        store: Any, tmp_path: pathlib.Path) -> None:
    drv = store.read_derivation(instantiate(tmp_path, LEAF))
    assert drv.name() == "leaf"
    assert drv.system() == "x86_64-linux"
    assert drv.builder() == "/bin/sh"
    assert drv.args() == ["-c", "echo"]
    assert drv.env()["FOO"] == "bar"
    assert drv.structured_attrs() is None


def test_an_input_addressed_output_names_its_path(
        store: Any, tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import DerivationOutputInputAddressed

    path = instantiate(tmp_path, LEAF)
    outputs = store.read_derivation(path).outputs()
    assert sorted(outputs) == ["dev", "out"]
    out = outputs["out"]
    assert isinstance(out, DerivationOutputInputAddressed)
    assert out.path() == store.query_derivation_output_map(path)["out"]


def test_a_fixed_output_carries_its_content_address(
        store: Any, tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import DerivationOutputCAFixed

    out = store.read_derivation(instantiate(tmp_path, FIXED)).outputs()["out"]
    assert isinstance(out, DerivationOutputCAFixed)
    assert out.ca().method() == "flat"
    assert out.ca().hash().algorithm() == "sha256"


def test_inputs_are_the_derivations_and_sources_it_needs(
        store: Any, tmp_path: pathlib.Path) -> None:
    leaf = instantiate(tmp_path, LEAF)
    root = store.read_derivation(instantiate(tmp_path, ROOT))
    [(name, node)] = root.input_drvs().items()
    assert name == leaf.to_string()
    assert node.outputs() == ["dev"]
    assert node.dynamic_outputs() == {}
    [src] = root.input_srcs()
    assert src.name() == "src"


@pytest.mark.usefixtures("dynamic_derivations")
def test_an_output_of_an_output_is_a_child_node(
        store: Any, tmp_path: pathlib.Path) -> None:
    [node] = store.read_derivation(
        instantiate(tmp_path, OUTER)).input_drvs().values()
    assert node.outputs() == []
    [(output, child)] = node.dynamic_outputs().items()
    assert output == "out"
    assert child.outputs() == ["out"]
    assert child.dynamic_outputs() == {}


def test_a_derivation_value_names_its_outputs(
        tmp_path: pathlib.Path) -> None:
    from huggorm_bindings import EvalState, Store

    store = Store(str(tmp_path))
    value = EvalState(store).eval_expr(LEAF, str(tmp_path))
    paths = value.output_paths()
    assert sorted(paths) == ["dev", "out"]
    assert paths == store.query_derivation_output_map(value.drv_path())


def test_a_floating_output_is_not_a_store_path_yet(
        tmp_path: pathlib.Path, dynamic_derivations: None) -> None:
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import EvalError

    value = EvalState(Store(str(tmp_path))).eval_expr(INNER, str(tmp_path))
    with pytest.raises(EvalError, match="is not in the Nix store"):
        value.output_paths()


def test_a_build_reads_its_derivation_from_the_eval_store(
        tmp_path: pathlib.Path) -> None:
    """A fixed output already valid in the build store needs no
    builder: only the `.drv`, which the eval store holds and the
    build store does not."""
    from huggorm_bindings import (
        ContentAddressMethod,
        DerivedPathBuilt,
        HashAlgorithm,
        OutputsSpec,
        Store,
    )

    built = Store(str(tmp_path / "build"))
    held = built.add_to_store("fixed", b"", ContentAddressMethod.FLAT,
                              HashAlgorithm.SHA256)
    ca = built.query_path_info(held).ca()
    assert ca is not None
    sri = ca.hash().sri()
    (tmp_path / "eval").mkdir()
    drv = instantiate(tmp_path / "eval", FIXED.replace(
        "sha256-47DEQpj8HBSa+/TWmW+nGUhGKf1Kq8BQi/ljyeGd3sQ=", sri))
    evaluated = Store(str(tmp_path / "eval"))
    target = DerivedPathBuilt(drv, OutputsSpec(names=["out"]))

    [result] = built.build_paths_with_results([target],
                                              eval_store=evaluated)
    assert result.error() is None
    assert not built.is_valid_path(drv)

    [alone] = built.build_paths_with_results([target])
    assert alone.error() is not None


def test_json_round_trips_to_the_same_path(
        store: Any, tmp_path: pathlib.Path) -> None:
    """`add` of what `show` printed writes the same `.drv`."""
    path = instantiate(tmp_path, ROOT)
    document = store.read_derivation(path).to_json()
    assert store.add_derivation(document) == path


def test_a_dev_shell_derivation_from_generated_operations(
        store: Any, tmp_path: pathlib.Path) -> None:
    """What `nix develop` does to a derivation, as one operation.

    Read it, change the document, add the script, write it back.
    `add_derivation` fills in the deferred output paths, so no hash
    is computed here. The builder dumps the environment when the
    answer builds, which nothing here does.
    """
    from huggorm.devshell import write_dev_shell_derivation
    from huggorm_bindings import DerivationOutputInputAddressed

    path = instantiate(tmp_path, BASH_LEAF)
    script = b"echo env\n"
    shell = store.read_derivation(
        write_dev_shell_derivation(store, path, script))

    assert shell.name() == "leaf-env"
    assert len(shell.args()) == 1 and shell.args()[0].endswith("get-env.sh")
    assert any(p.to_string().endswith("get-env.sh")
               for p in shell.input_srcs())
    for output in shell.outputs().values():
        assert isinstance(output, DerivationOutputInputAddressed)
    assert shell.outputs()["out"].path() != \
        store.read_derivation(path).outputs()["out"].path()


async def test_an_async_dev_shell_derivation_rewrites(
        store: Any, tmp_path: pathlib.Path) -> None:
    """The async flavour, over a local `AsyncStore`."""
    from huggorm.devshell import awrite_dev_shell_derivation
    from huggorm_bindings import DerivationOutputInputAddressed
    from huggorm_generated import AsyncStore

    path = instantiate(tmp_path, BASH_LEAF)
    shell_path = await awrite_dev_shell_derivation(
        AsyncStore(str(tmp_path)), path, b"echo env\n")
    shell = store.read_derivation(shell_path)

    assert shell.name() == "leaf-env"
    for output in shell.outputs().values():
        assert isinstance(output, DerivationOutputInputAddressed)


def test_a_dev_shell_derivation_refuses_a_non_bash_builder(
        store: Any, tmp_path: pathlib.Path) -> None:
    """The refusal `nix develop` makes, at rewrite time.

    LEAF builds with `/bin/sh`, so rewriting it fails here rather
    than producing a derivation that fails obscurely at build time.
    """
    from huggorm.devshell import write_dev_shell_derivation
    from huggorm_bindings.errors import NixError

    path = instantiate(tmp_path, LEAF)
    with pytest.raises(NixError, match="bash"):
        write_dev_shell_derivation(store, path, b"echo env\n")


def _leaf_document(**overrides: Any) -> dict[str, Any]:
    """The document shape `to_json` answers, without a store."""
    document: dict[str, Any] = {
        "name": "leaf",
        "outputs": {"out": {"path": "/nix/store/x-out"}},
        "inputs": {"drvs": [], "srcs": []},
        "builder": "/bin/bash",
        "args": ["-c", "echo"],
        "env": {"name": "leaf", "out": "/nix/store/x-out"},
    }
    document.update(overrides)
    return document


def test_a_dev_shell_rewrite_strips_flat_reference_checks() -> None:
    """A shell answers no reference checks, so none cross."""
    from huggorm.devshell import _rewrite

    env = {"name": "leaf", "allowedReferences": ["x"],
           "allowedRequisites": ["x"], "disallowedReferences": ["x"],
           "disallowedRequisites": ["x"], "KEEP": "yes"}
    out = _rewrite(_leaf_document(env=env), "/nix/store/s.sh", "/nix/store/s.sh")
    assert out["env"] == {"name": "leaf-env", "KEEP": "yes", "out": ""}


def test_a_dev_shell_rewrite_strips_structured_output_checks() -> None:
    """The structured shape of the same rule."""
    from huggorm.devshell import _rewrite

    attrs = {"outputChecks": {"out": {}}, "other": True}
    out = _rewrite(_leaf_document(structuredAttrs=attrs),
                   "/nix/store/s.sh", "/nix/store/s.sh")
    assert out["structuredAttrs"] == {"other": True}


def test_a_dev_shell_rewrite_keeps_outputs_without_a_path() -> None:
    """Only addressed outputs invalidate: the rest have no path."""
    from huggorm.devshell import _rewrite

    outputs = {"out": {"path": "/nix/store/x-out"},
               "fixed": {"hash": "sha256:abc"},
               "float": {"CAFloating": True}}
    env = {"name": "leaf", "out": "/nix/store/x-out",
           "fixed": "/nix/store/x-fixed", "float": "keep"}
    out = _rewrite(_leaf_document(outputs=outputs, env=env),
                   "/nix/store/s.sh", "/nix/store/s.sh")
    assert out["outputs"] == {"out": {}, "fixed": {},
                              "float": {"CAFloating": True}}
    assert out["env"]["out"] == "" and out["env"]["fixed"] == ""
    assert out["env"]["float"] == "keep"


def test_the_package_carries_nix_s_get_env_script() -> None:
    """`nix develop`'s builder script, which no Nix library holds."""
    script = importlib.resources.files("huggorm_bindings") / "get-env.sh"
    assert "__dumpEnv() {" in script.read_text()
