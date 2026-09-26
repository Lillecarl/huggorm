"""A `.drv`, read and written: `nix derivation show` and `add`.

The derivations are instantiated by evaluating them against a chroot
store, which writes the `.drv` and needs no builder.
"""

import json
import pathlib
from typing import Any

import pytest

LEAF = ('derivation { name = "leaf"; system = "x86_64-linux"; '
        'builder = "/bin/sh"; args = [ "-c" "echo" ]; '
        'outputs = [ "out" "dev" ]; FOO = "bar"; }')
ROOT = (f'derivation {{ name = "root"; system = "x86_64-linux"; '
        f'builder = "/bin/sh"; dep = ({LEAF}).dev; src = ./src; }}')
FIXED = ('derivation { name = "fixed"; system = "x86_64-linux"; '
         'builder = "/bin/sh"; outputHashMode = "flat"; '
         'outputHashAlgo = "sha256"; outputHash = '
         '"sha256-47DEQpj8HBSa+/TWmW+nGUhGKf1Kq8BQi/ljyeGd3sQ="; }')


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Any:
    from huggorm_bindings import Store

    return Store(str(tmp_path))


def instantiate(tmp_path: pathlib.Path, expr: str) -> Any:
    from huggorm_bindings import EvalState

    (tmp_path / "src").write_text("source\n")
    return EvalState(str(tmp_path)).eval_expr(expr, str(tmp_path)).drv_path()


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
    assert root.input_drvs() == {leaf.to_string(): ["dev"]}
    [src] = root.input_srcs()
    assert src.name() == "src"


def test_json_round_trips_to_the_same_path(
        store: Any, tmp_path: pathlib.Path) -> None:
    """`add` of what `show` printed writes the same `.drv`."""
    path = instantiate(tmp_path, ROOT)
    document = store.read_derivation(path).to_json()
    assert store.add_derivation(document) == path


def test_a_dev_shell_derivation_from_generated_operations(
        store: Any, tmp_path: pathlib.Path) -> None:
    """What `nix develop` does to a derivation, done in Python over
    the generated operations only: read it, change the document, add
    a script, write it back. `add_derivation` fills in the deferred
    output paths, so no hash is computed here."""
    from huggorm_bindings import (
        ContentAddressMethod,
        DerivationOutputInputAddressed,
        HashAlgorithm,
    )

    path = instantiate(tmp_path, LEAF)
    document = json.loads(store.read_derivation(path).to_json())
    script = store.add_to_store("get-env.sh", b"echo env\n",
                                ContentAddressMethod.TEXT,
                                HashAlgorithm.SHA256)
    document["name"] += "-env"
    document["env"]["name"] = document["name"]
    document["args"] = [store.print_store_path(script)]
    document["inputs"]["srcs"].append(script.to_string())
    for name in document["outputs"]:
        document["outputs"][name] = {}
        document["env"][name] = ""

    shell = store.read_derivation(store.add_derivation(json.dumps(document)))

    assert shell.name() == "leaf-env"
    assert script in shell.input_srcs()
    for output in shell.outputs().values():
        assert isinstance(output, DerivationOutputInputAddressed)
    assert shell.outputs()["out"].path() != \
        store.read_derivation(path).outputs()["out"].path()
