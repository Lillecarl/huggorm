"""nix.conf reaches an evaluator, once the caller loads it.

`EvalSettings` is not a libstore object, so nothing registers it on
`globalConfig` unless this repo does - `nix` does it from libcmd,
which is not linked. Unregistered, `pure-eval` in the file is parked
as an unknown setting and never read, with no warning (tasks/097).

A fresh interpreter per case, because the file is read once, at
import, and the process keeps what it read.
"""

import json
import os
import pathlib
import subprocess
import sys
from collections.abc import Callable, Iterator

import pytest

PROBE = """
import sys
from huggorm_bindings import EvalState, load_config
if sys.argv[1] == "load":
    load_config()
print(EvalState("dummy://").eval_expr("builtins ? currentTime").boolean())
"""


def has_current_time(nix_config: str, *, load: bool) -> bool:
    env = {**os.environ, "NIX_CONFIG": nix_config}
    argv = [sys.executable, "-c", PROBE, "load" if load else "skip"]
    out = subprocess.run(argv, env=env, capture_output=True, text=True,
                         check=True)
    return {"True": True, "False": False}[out.stdout.strip()]


def test_pure_eval_from_the_config_reaches_the_state() -> None:
    """`nix eval` answers false here, and so must a state."""
    assert has_current_time("pure-eval = true", load=True) is False


def test_import_alone_reads_no_config() -> None:
    """A library does not take the host's configuration because it was
    imported. `load_config` is the caller's choice."""
    assert has_current_time("pure-eval = true", load=False) is True


def test_nix_path_from_the_environment_reaches_the_state(
        tmp_path: pathlib.Path) -> None:
    """`initGC` copies NIX_PATH into the `nix-path` setting, through
    `globalConfig`. So it was lost with pure-eval: before the settings
    were registered, `<probe>` did not resolve."""
    (tmp_path / "default.nix").write_text("42")
    probe = ("from huggorm_bindings import EvalState;"
             "print(EvalState('dummy://').eval_expr('import <probe>')"
             ".integer())")
    env = {**os.environ, "NIX_CONFIG": "", "NIX_PATH": f"probe={tmp_path}"}
    out = subprocess.run([sys.executable, "-c", probe], env=env,
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "42"


def test_a_state_takes_its_own_search_path(tmp_path: pathlib.Path) -> None:
    """No constructor parameter: `nix-path` is an evaluator setting."""
    from huggorm_bindings import EvalState

    (tmp_path / "default.nix").write_text("7")
    state = EvalState("dummy://", {"nix-path": f"probe={tmp_path}"})
    assert state.eval_expr("import <probe>").integer() == 7


def test_the_control_has_current_time() -> None:
    """Without it the builtin exists, so the case above can fail."""
    assert has_current_time("", load=True) is True


@pytest.mark.parametrize("value", ["true", "false"])
def test_an_explicit_value_is_the_one_used(value: str) -> None:
    """A registration that only turned purity ON would pass the first
    case. Both values have to come through as written."""
    assert has_current_time(f"pure-eval = {value}", load=True) is (value == "false")


@pytest.fixture
def setting() -> Iterator[Callable[[str, str], None]]:
    """`set_setting`, with every name it touched put back afterwards.

    A setting belongs to the process, so a value left behind would
    change every later test in this run."""
    from huggorm_bindings import get_setting, set_setting

    saved: dict[str, str] = {}

    def set_(name: str, value: str) -> None:
        if name not in saved:
            before = get_setting(name)
            assert before is not None, name
            saved[name] = before
        set_setting(name, value)

    try:
        yield set_
    finally:
        for name, value in saved.items():
            set_setting(name, value)


def test_an_eval_setting_is_known_before_any_state() -> None:
    """The startup registers it; no `EvalState` has to exist first.

    A fresh interpreter, because building a state registers them too,
    and every earlier test file in a run has built one."""
    probe = ("from huggorm_bindings import get_setting;"
             "print(get_setting('pure-eval'))")
    out = subprocess.run([sys.executable, "-c", probe],
                         env={**os.environ, "NIX_CONFIG": ""},
                         capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "false"


def test_a_store_setting_is_there_too() -> None:
    from huggorm_bindings import list_settings

    names = list_settings()
    assert "pure-eval" in names
    assert "sandbox" in names
    assert "warn-dirty" in names, "fetcher settings are registered"


def test_an_unknown_name_reads_as_none() -> None:
    from huggorm_bindings import get_setting

    assert get_setting("no-such-setting") is None


def test_an_unknown_name_refuses_to_be_set() -> None:
    """`GlobalConfig::set` answers false and says nothing."""
    from huggorm_bindings import set_setting
    from huggorm_bindings.errors import UsageError

    with pytest.raises(UsageError, match="no-such-setting"):
        set_setting("no-such-setting", "1")


def test_a_set_value_reaches_the_next_state(
        setting: Callable[[str, str], None]) -> None:
    from huggorm_bindings import EvalState

    probe = "builtins ? currentTime"
    assert EvalState("dummy://").eval_expr(probe).boolean() is True
    setting("pure-eval", "true")
    assert EvalState("dummy://").eval_expr(probe).boolean() is False


def test_overridden_only_names_what_was_set(
        setting: Callable[[str, str], None]) -> None:
    from huggorm_bindings import list_settings

    setting("pure-eval", "true")
    overridden = list_settings(overridden_only=True)
    assert overridden["pure-eval"] == "true"
    assert len(overridden) < len(list_settings())


def test_extra_appends_to_a_list(setting: Callable[[str, str], None]) -> None:
    """How a caller enables an experimental feature: no function of its
    own, because `Config::set` takes nix.conf's `extra-` prefix."""
    from huggorm_bindings import get_setting, set_setting

    current = get_setting("experimental-features") or ""
    before = current.split()
    # Saved under the name `get_setting` answers to; `extra-` is none.
    setting("experimental-features", current)
    set_setting("extra-experimental-features", "fetch-closure")
    after = (get_setting("experimental-features") or "").split()
    assert "fetch-closure" in after
    assert set(before) <= set(after)


def test_the_json_is_nix_s_own_description() -> None:
    from huggorm_bindings import settings_json

    described = json.loads(settings_json())
    assert described["pure-eval"]["value"] is False
    assert "description" in described["pure-eval"]


def test_a_state_takes_its_own_settings() -> None:
    """Over what the process has, and for that state alone."""
    from huggorm_bindings import EvalState

    probe = "builtins ? currentTime"
    pure = EvalState("dummy://", {"pure-eval": "true"})
    assert pure.eval_expr(probe).boolean() is False
    assert EvalState("dummy://").eval_expr(probe).boolean() is True


def test_a_state_setting_beats_the_process_one(
        setting: Callable[[str, str], None]) -> None:
    from huggorm_bindings import EvalState

    setting("pure-eval", "true")
    impure = EvalState("dummy://", settings={"pure-eval": "false"})
    assert impure.eval_expr("builtins ? currentTime").boolean() is True


def test_a_fetcher_setting_is_a_state_setting_too() -> None:
    from huggorm_bindings import EvalState

    EvalState("dummy://", {"warn-dirty": "false"})


@pytest.mark.parametrize("name", ["no-such-setting", "sandbox"])
def test_a_name_the_state_does_not_hold_refuses(name: str) -> None:
    """`sandbox` is a store setting, and a state has none of its own:
    taking it would change nothing and say nothing."""
    from huggorm_bindings import EvalState
    from huggorm_bindings.errors import UsageError

    with pytest.raises(UsageError, match=name):
        EvalState("dummy://", {name: "false"})


def test_the_current_system_is_what_the_evaluator_answers() -> None:
    from huggorm_bindings import EvalState, current_system

    answered = EvalState("dummy://").eval_expr("builtins.currentSystem")
    assert current_system() == answered.string_value()


def test_eval_system_moves_the_current_system(
        setting: Callable[[str, str], None]) -> None:
    from huggorm_bindings import current_system

    setting("eval-system", "riscv64-linux")
    assert current_system() == "riscv64-linux"


def test_the_version_is_the_evaluator_s_own() -> None:
    from huggorm_bindings import EvalState, nix_version

    answered = EvalState("dummy://").eval_expr("builtins.nixVersion")
    assert nix_version() == answered.string_value()


def test_this_build_has_the_collector() -> None:
    """`default.nix` builds libexpr with Boehm, as nixpkgs does."""
    from huggorm_bindings import boehm_gc

    assert boehm_gc() is True


def test_reset_forgets_the_mark_and_keeps_the_value(
        setting: Callable[[str, str], None]) -> None:
    from huggorm_bindings import get_setting, list_settings, reset_overridden

    setting("warn-dirty", "false")
    assert "warn-dirty" in list_settings(overridden_only=True)
    reset_overridden()
    assert "warn-dirty" not in list_settings(overridden_only=True)
    assert get_setting("warn-dirty") == "false"


def test_a_url_in_the_search_path_is_one_entry() -> None:
    from huggorm_bindings import parse_nix_path

    assert parse_nix_path("a=/x:nixpkgs=https://example.org/n.tar.gz:/y") \
        == ["a=/x", "nixpkgs=https://example.org/n.tar.gz", "/y"]
    assert parse_nix_path("") == []


def test_a_url_is_a_pseudo_url_and_a_path_is_not() -> None:
    from huggorm_bindings import is_pseudo_url

    assert is_pseudo_url("https://example.org/n.tar.gz") is True
    assert is_pseudo_url("/nix/store") is False


def test_a_store_uri_renders_as_nix_normalises_it() -> None:
    from huggorm_bindings import render_store_reference

    uri = "local?root=/tmp/x"
    assert render_store_reference(uri) == "local://?root=/tmp/x"
    assert render_store_reference(uri, with_params=False) == "local://"
