"""nix.conf reaches an evaluator.

`EvalSettings` is not a libstore object, so nothing registers it on
`globalConfig` unless this repo does - `nix` does it from libcmd,
which is not linked. Unregistered, `pure-eval` in the file is parked
as an unknown setting and never read, with no warning (tasks/097).

A fresh interpreter per case, because the file is read once, at
import, and the process keeps what it read.
"""

import os
import subprocess
import sys

import pytest

PROBE = """
from huggorm_bindings import EvalState
print(EvalState("dummy://").eval_expr("builtins ? currentTime").boolean())
"""


def has_current_time(nix_config: str) -> bool:
    env = {**os.environ, "NIX_CONFIG": nix_config}
    out = subprocess.run([sys.executable, "-c", PROBE], env=env,
                         capture_output=True, text=True, check=True)
    return {"True": True, "False": False}[out.stdout.strip()]


def test_pure_eval_from_the_config_reaches_the_state() -> None:
    """`nix eval` answers false here, and so must a state."""
    assert has_current_time("pure-eval = true") is False


def test_the_control_has_current_time() -> None:
    """Without it the builtin exists, so the case above can fail."""
    assert has_current_time("") is True


@pytest.mark.parametrize("value", ["true", "false"])
def test_an_explicit_value_is_the_one_used(value: str) -> None:
    """A registration that only turned purity ON would pass the first
    case. Both values have to come through as written."""
    assert has_current_time(f"pure-eval = {value}") is (value == "false")
