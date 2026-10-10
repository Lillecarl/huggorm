"""The DSL's version names, against the Nix versions the build has.

`nix/versions.nix` lists the versions. A type checker needs each
`NIX_2_3x` name written out in `declare.py`, so the names stay literal
there, and these tests hold them to the list the build passes in.
"""

import os
import re

import pytest

from huggorm_dsl import declare


def _from_build(name: str) -> str:
    value = os.environ.get(name)
    if value is None:
        pytest.fail(f"{name} is unset: `generator-tests` in default.nix sets it")
    return value


def test_every_gate_has_a_name_and_no_other_name_exists() -> None:
    gates = _from_build("HUGGORM_NIX_GATES").split()
    names = {name for name in vars(declare) if re.fullmatch(r"NIX_\d+_\d+", name)}
    assert names == {"NIX_" + gate.replace(".", "_") for gate in gates}


def test_an_unset_version_is_the_base(monkeypatch: pytest.MonkeyPatch) -> None:
    base = _from_build("HUGGORM_NIX_BASE")
    monkeypatch.delenv("HUGGORM_NIX_VERSION", raising=False)
    assert ".".join(map(str, declare._nix_version())) == base
