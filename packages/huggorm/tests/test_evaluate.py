"""What `nix eval` and `nix build` need from an evaluated value.

`Value.drv_path` joins evaluation to building: `DerivedPathBuilt`
needs the `.drv` a derivation names. `to_json` is `nix eval --json`,
and `base` is the directory a relative path names from.

The state runs against a chroot store, because reading `drvPath` and
copying a path both write to it.
"""

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest

DRV = ('derivation { name = "joined"; system = "x86_64-linux"; '
       'builder = "/bin/sh"; }')


@pytest.fixture
def state(tmp_path: pathlib.Path) -> Any:
    from huggorm_bindings import EvalState, Store

    return EvalState(Store(str(tmp_path)))


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


def test_the_wrong_type_is_nix_s_own_type_error(state: Any) -> None:
    """`forceAttrs` refuses a number with this class and these words, so a
    caller catches one error for Nix and for the binding."""
    from huggorm_bindings.errors import NixTypeError

    with pytest.raises(NixTypeError,
                       match="expected a set but found an integer"):
        state.eval_expr("1").has("x")
    items = state.make_attrs()
    with pytest.raises(NixTypeError, match="expected a list but found a set"):
        state.list_append(items, state.make_int(1))


def test_a_size_of_neither_collection_is_a_type_error(state: Any) -> None:
    from huggorm_bindings.errors import NixTypeError

    with pytest.raises(NixTypeError,
                       match="expected a list or a set but found an integer"):
        state.eval_expr("1").size()


def test_length_and_names_each_take_one_collection(state: Any) -> None:
    """`size` answers for either; these refuse the other one, even
    when it is empty."""
    from huggorm_bindings.errors import NixTypeError

    assert state.eval_expr("[ 1 2 ]").length() == 2
    assert state.eval_expr("{ b = 1; a = 2; }").names() == ["a", "b"]
    with pytest.raises(NixTypeError, match="expected a list but found a set"):
        state.eval_expr("{ }").length()
    with pytest.raises(NixTypeError, match="expected a set but found a list"):
        state.eval_expr("[ ]").names()


def test_a_missing_attribute_is_nix_s_own_error(state: Any) -> None:
    """The error `{ foo = 1; }.fo` raises, with the suggestion Nix
    ranks from the set's own names."""
    from huggorm_bindings.errors import MissingAttribute

    with pytest.raises(MissingAttribute,
                       match="attribute 'fo' missing") as caught:
        state.eval_expr("{ foo = 1; bar = 2; }").get("fo")
    assert "Did you mean foo?" in str(caught.value)


def test_a_missing_attribute_carries_its_suggestions(state: Any) -> None:
    """The ranking travels as data too, the best match first."""
    from huggorm_bindings.errors import MissingAttribute

    with pytest.raises(MissingAttribute) as caught:
        state.eval_expr("{ foo = 1; bar = 2; }").get("fo")
    info = caught.value.info
    assert info is not None
    assert info.suggestions()[0] == "foo"


def test_a_thrown_message_in_no_encoding_survives_translation(
        state: Any, tmp_path: pathlib.Path) -> None:
    """The translator decodes lossily, so the error survives it.

    `throw` carries whatever string it was given, and a file holds
    whatever bytes it holds. Strict decoding would fail the
    translation on the first such byte and lose the error to a
    `UnicodeDecodeError` - so the message reads with U+FFFD, and the
    exact bytes stay on the info.
    """
    from huggorm_bindings.errors import ThrownError

    blob = tmp_path / "blob"
    blob.write_bytes(b"\xff\xfe")
    with pytest.raises(ThrownError) as caught:
        state.eval_expr(f'builtins.throw (builtins.readFile "{blob}")')
    assert "\ufffd" in str(caught.value)
    info = caught.value.info
    assert info is not None
    with pytest.raises(UnicodeDecodeError):
        info.msg()
    assert info.msg_bytes() == b"\xff\xfe"


def test_a_suggestion_in_no_encoding_reads_as_bytes(
        state: Any, tmp_path: pathlib.Path) -> None:
    """A suggestion is an attr name, and names are bytes too.

    The set holds a quoted name no decoder accepts, so looking up a
    near miss suggests it raw. `suggestions` fails the strict read;
    `suggestions_bytes` answers it.
    """
    from huggorm_bindings.errors import MissingAttribute

    src = tmp_path / "s.nix"
    src.write_bytes(b'{ "caf\xe9" = 1; }')
    value = state.eval_file(str(src))
    with pytest.raises(MissingAttribute) as caught:
        value.get("caf")
    info = caught.value.info
    assert info is not None
    with pytest.raises(UnicodeDecodeError):
        info.suggestions()
    assert info.suggestions_bytes() == [b"caf\xe9"]


def test_a_position_in_no_encoding_reads_as_bytes(
        state: Any, tmp_path: pathlib.Path) -> None:
    """A position names a file, and filenames are bytes.

    The path cannot cross into `eval_file` as text, so a wrapper
    imports it: the raw name is built inside Nix, never decoded. The
    error positioned there fails both strict reads.
    """
    import os

    from huggorm_bindings.errors import EvalError

    inner = os.fsencode(str(tmp_path)) + b"/caf\xe9.nix"
    with open(inner, "wb") as f:
        f.write(b'1 + "a"\n')
    wrapper = tmp_path / "wrapper.nix"
    wrapper.write_bytes(b'import (./. + "/caf\xe9.nix")')
    with pytest.raises(EvalError) as caught:
        state.eval_file(str(wrapper))
    info = caught.value.info
    assert info is not None
    pos = info.pos()
    assert pos is not None
    with pytest.raises(UnicodeDecodeError):
        pos.file()
    assert pos.file_bytes().endswith(b"caf\xe9.nix")
    assert any(b"caf\xe9.nix" in t.hint_bytes() for t in info.traces())


def test_an_error_carries_its_position(state: Any) -> None:
    """C++ is the only place that holds where an error is (huggorm#100)."""
    from huggorm_bindings.errors import EvalError

    with pytest.raises(EvalError) as caught:
        state.eval_expr('let\n  x = 1;\nin\n  x + "a"')
    info = caught.value.info
    assert info is not None
    pos = info.pos()
    assert pos is not None
    assert (pos.file(), pos.line(), pos.column()) == ("«string»", 4, 7)
    assert "cannot add" in info.msg()


def test_a_deep_trace_keeps_the_frames_nearest_the_error(state: Any) -> None:
    """32 frames, the last of them the innermost, and a flag for the
    rest: the error crosses the wire in a status header."""
    from huggorm_bindings.errors import EvalError

    deep = ('let f = n: if n == 0 then throw "x" else { a = f (n - 1); }; '
            "in builtins.toJSON (f 40)")
    with pytest.raises(EvalError) as caught:
        state.eval_expr(deep).string_value()
    info = caught.value.info
    assert info is not None
    assert len(info.traces()) == 32
    assert info.truncated()
    assert "throw" in info.traces()[-1].hint()
    assert info.is_from_expr()


@pytest.mark.parametrize(("expr", "name"), [
    ('throw "boom"', "ThrownError"),
    ("assert false; 1", "NixAssertionError"),
    ('abort "stop"', "Abort"),
    ("undefined_name", "UndefinedVarError"),
    ("1 +", "ParseError"),
])
def test_each_evaluation_error_raises_its_own_class(
        state: Any, expr: str, name: str) -> None:
    """The class libexpr throws, so `except ThrownError` means `throw`
    and nothing else."""
    from huggorm_bindings import errors

    with pytest.raises(errors.NixError) as caught:
        state.eval_expr(expr)
    assert type(caught.value).__name__ == name


def test_the_classes_nest_as_libexpr_s_do() -> None:
    """A `throw` is an assertion is an evaluation error; a parse error
    is none of them."""
    from huggorm_bindings import errors

    assert issubclass(errors.ThrownError, errors.NixAssertionError)
    assert issubclass(errors.NixAssertionError, errors.EvalError)
    assert issubclass(errors.Abort, errors.EvalError)
    assert not issubclass(errors.ParseError, errors.EvalBaseError)


def test_an_error_python_builds_carries_no_info() -> None:
    from huggorm_bindings.errors import NixError

    assert NixError("made here").info is None


def test_an_index_past_the_end_names_both_numbers(state: Any) -> None:
    from huggorm_bindings.errors import EvalError, ListIndex

    items = state.eval_expr("[ 1 2 ]")
    for index in (2, -1):
        with pytest.raises(ListIndex, match=f"list index {index} is out of "
                           "bounds for a list of size 2") as caught:
            items.at(index)
        assert isinstance(caught.value, EvalError)


def test_an_attribute_set_that_is_not_a_derivation_refuses(
        state: Any) -> None:
    from huggorm_bindings.errors import NixError

    # Nix words this per caller; this is nanopynix's, which pynix reads.
    with pytest.raises(NixError, match="selected value is not a derivation"):
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
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import NixError

    state = EvalState(Store(str(tmp_path)),
                      {"allow-import-from-derivation": "false"})
    with pytest.raises(NixError) as caught:
        state.eval_expr(f'"${{{DRV}}}"').realise_string()
    assert "allow-import-from-derivation" not in str(caught.value)


def test_a_build_store_is_taken_with_the_state(
        tmp_path: pathlib.Path) -> None:
    """A state accepts a second store to build in. No build runs here,
    because the gate's sandbox cannot run a builder."""
    from huggorm_bindings import EvalState, Store

    state = EvalState(Store(str(tmp_path / "evals")), None,
                      Store(str(tmp_path / "builds")))
    assert state.eval_expr("1 + 1").integer() == 2


def test_the_state_shares_its_store() -> None:
    """The state evaluates against the store it was given, not a
    second one opened from the same URI. `dummy://` shows the
    difference: each one opened is a separate, empty store. It refuses
    a write unless `read-only=false`."""
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import NixError

    uri = "dummy://?read-only=false"
    store = Store(uri)
    path = store.add_to_store("greeting", b"hello\n")
    full = f"{store.store_dir()}/{path.to_string()}"
    probe = f'builtins.storePath "{full}"'
    assert EvalState(store).eval_expr(probe).string_value() == full
    with pytest.raises(NixError, match="no substituter"):
        EvalState(Store(uri)).eval_expr(probe)


def test_a_derivation_s_output_is_in_the_string_s_context(state: Any) -> None:
    """`"${drv}"` owes the store that derivation's output, and every
    string that holds it, however deep, shares the debt."""
    value = state.eval_expr(f'{{ a = [ "x${{{DRV}}}" ]; b = "plain"; }}')
    context = value.string_context()
    assert len(context) == 1
    # Nix's encoding: the output, then the derivation's base name.
    assert context[0].startswith("!out!")
    assert context[0].endswith("-joined.drv")


def test_a_string_made_with_a_context_keeps_it(state: Any) -> None:
    held = state.eval_expr(f'"${{{DRV}}}"').string_context()
    made = state.make_string("anything", held)
    assert made.string_context() == held
    assert state.make_string("anything").string_context() == []


def test_realised_json_names_what_the_context_holds(state: Any) -> None:
    """Nothing to build here, so the JSON is `to_json`'s; the context
    of a path already in the store realises to itself."""
    added = state.eval_expr('builtins.toFile "note" "hi"')
    assert added.realise_json() == added.to_json()
    assert added.string_context()


# 50 multiplications, and a fold over the 50 results.
COUNTED = "builtins.foldl' (a: b: a + b) 0 (builtins.genList (i: i * 2) 50)"


@pytest.fixture
def counting() -> Iterator[None]:
    """The evaluation counters on for one test, and put back after.

    The switch belongs to the process, so a value left behind would
    change every later test."""
    from huggorm_bindings import eval_counters_enabled, set_eval_counters_enabled

    before = eval_counters_enabled()
    set_eval_counters_enabled(True)
    try:
        yield
    finally:
        set_eval_counters_enabled(before)


def test_the_counters_switch_reads_back() -> None:
    from huggorm_bindings import eval_counters_enabled, set_eval_counters_enabled

    before = eval_counters_enabled()
    try:
        set_eval_counters_enabled(not before)
        assert eval_counters_enabled() is (not before)
    finally:
        set_eval_counters_enabled(before)


def test_statistics_count_an_evaluation(state: Any, counting: None) -> None:
    """The numeric fields move with the work, once the counters are on."""
    del counting
    before = json.loads(state.statistics_json())
    assert state.eval_expr(COUNTED).integer() == 2450
    after = json.loads(state.statistics_json())
    assert after["nrFunctionCalls"] > before["nrFunctionCalls"]
    assert after["values"]["number"] > before["values"]["number"]


def test_the_call_tables_need_count_calls(tmp_path: pathlib.Path) -> None:
    """`count-calls` fills the tables, and without it they are absent."""
    from huggorm_bindings import EvalState, Store

    store = Store(str(tmp_path))
    plain = EvalState(store)
    plain.eval_expr(COUNTED)
    assert "primops" not in json.loads(plain.statistics_json())

    counted = EvalState(store, {"count-calls": "true"})
    counted.eval_expr(COUNTED)
    assert json.loads(counted.statistics_json())["primops"]["mul"] == 50
