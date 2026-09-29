"""A REPL scope (`repl.cc`): bindings that later expressions see."""

import pathlib
import threading
from typing import Any

import pytest
from nixversion import needs_collector

from huggorm_bindings import (
    EvalState,
    Repl,
    Store,
    collect_garbage,
    gc_release_thread,
    gc_stats,
)
from huggorm_bindings.errors import EvalError, NixError


@pytest.fixture
def state(tmp_path: pathlib.Path) -> EvalState:
    return EvalState(Store(str(tmp_path)))


@pytest.fixture
def repl(state: EvalState) -> Repl:
    return state.repl()


def test_a_binding_is_seen_by_a_later_line(repl: Repl) -> None:
    assert repl.process_line("x = 41") is None
    answer = repl.process_line("x + 1")
    assert answer is not None
    assert answer.integer() == 42


def test_a_binding_is_lazy(repl: Repl) -> None:
    assert repl.process_line('x = throw "not yet"') is None
    with pytest.raises(EvalError, match="not yet"):
        repl.eval_expr("x")


def test_inherit_without_a_semicolon_binds(repl: Repl) -> None:
    repl.process_line("a = { b = 3; }")
    assert repl.process_line("inherit (a) b") is None
    assert repl.eval_expr("b").integer() == 3


def test_a_rebinding_shadows_and_keeps_the_old_thunk(repl: Repl) -> None:
    repl.process_line("x = 1")
    repl.process_line("y = x")
    repl.process_line("x = 2")
    assert (repl.eval_expr("x").integer(), repl.eval_expr("y").integer()) == (2, 1)


def test_scopes_are_apart(state: EvalState, repl: Repl) -> None:
    repl.process_line("x = 1")
    with pytest.raises(EvalError, match="undefined variable 'x'"):
        state.repl().eval_expr("x")
    with pytest.raises(EvalError, match="undefined variable 'x'"):
        state.eval_expr("x")


def test_added_attributes_are_named_and_bound(repl: Repl) -> None:
    added = repl.add_attrs(repl.eval_expr("{ p = 1; q = 2; }"))
    assert sorted(added) == ["p", "q"]
    assert repl.eval_expr("p + q").integer() == 3


def test_adding_a_non_set_is_nix_s_type_error(repl: Repl) -> None:
    with pytest.raises(EvalError, match="expected a set but found an integer"):
        repl.add_attrs(repl.eval_expr("1"))


def test_the_names_hold_the_bindings_and_the_base_scope(repl: Repl) -> None:
    repl.process_line("mine = 1")
    names = repl.names()
    assert {"mine", "builtins", "true", "import"} <= set(names)
    assert names == sorted(set(names))


def test_a_select_splits_before_its_last_name(repl: Repl) -> None:
    repl.process_line('a = { b = { c = 1; }; n = "c"; }')
    selected = repl.select("a.b.c")
    assert selected is not None
    assert selected.name() == "c"
    assert selected.attrs().has("c")
    dynamic = repl.select("a.b.${a.n}")
    assert dynamic is not None
    assert dynamic.name() == "c"


def test_a_non_select_selects_nothing(repl: Repl) -> None:
    assert repl.select("1 + 1") is None


def test_a_file_sees_the_bindings(repl: Repl, tmp_path: pathlib.Path) -> None:
    (tmp_path / "default.nix").write_text("x + 1")
    repl.process_line("x = 41")
    assert repl.eval_file(str(tmp_path)).integer() == 42


def test_a_file_is_found_in_the_lookup_path(tmp_path: pathlib.Path) -> None:
    (tmp_path / "expressions").mkdir()
    (tmp_path / "expressions" / "default.nix").write_text("x + 1")
    (tmp_path / "plain.nix").write_text("{ y ? 1 }: y")
    lookup = f"example={tmp_path / 'expressions'} plain={tmp_path / 'plain.nix'}"
    state = EvalState(Store(str(tmp_path / "store")), {"nix-path": lookup})
    repl = state.repl()
    repl.process_line("x = 41")
    assert repl.eval_file("<example>").integer() == 42
    assert state.eval_file("<plain>").type_name() == "function"
    assert repl.load_file("<plain>").integer() == 1


def test_a_loaded_file_is_called_and_not_added(
        repl: Repl, tmp_path: pathlib.Path) -> None:
    (tmp_path / "default.nix").write_text("{ value ? 41 }: { answer = value + 1; }")
    loaded = repl.load_file(str(tmp_path))
    assert loaded.type_name() == "attrs"
    assert "answer" not in repl.names()
    repl.add_attrs(loaded)
    assert repl.eval_expr("answer").integer() == 42


REPL_SIZE = 32768


def _set_of(count: int) -> str:
    return (f"builtins.listToAttrs (builtins.genList"
            f' (i: {{ name = "a" + toString i; value = i; }}) {count})')


def test_a_set_that_fills_the_scope_exactly_is_added(repl: Repl) -> None:
    assert len(repl.add_attrs(repl.eval_expr(_set_of(REPL_SIZE)))) == REPL_SIZE


def test_one_binding_past_the_end_is_nix_s_refusal(repl: Repl) -> None:
    with pytest.raises(NixError, match="environment full; cannot add more variables"):
        repl.add_attrs(repl.eval_expr(_set_of(REPL_SIZE + 1)))
    repl.add_attrs(repl.eval_expr(_set_of(REPL_SIZE)))
    with pytest.raises(NixError, match="environment full"):
        repl.process_line("one = 1")


def _scope_made_elsewhere(state: EvalState) -> list[Repl]:
    """A scope made on a thread that then exits. The collector scans
    stacks conservatively, so a stale copy of the pointer on this
    thread's stack would keep the environment alive with no root."""
    made: list[Repl] = []

    def make() -> None:
        made.append(state.repl())
        made[0].process_line("x = { a = 42; }")
        gc_release_thread()

    worker = threading.Thread(target=make)
    worker.start()
    worker.join()
    return made


def _collected_after_churn(state: EvalState) -> int:
    collect_garbage()
    state.eval_expr("builtins.length (builtins.genList (i: { a = i; }) 200000)")
    collect_garbage()
    return gc_stats()["scopes_collected"]


@needs_collector
def test_a_held_scope_is_never_collected(state: EvalState) -> None:
    """Reading a binding back cannot show this: a freed block keeps its
    contents until it is handed out again. The count of finalized
    environments can, and the control shows the count moves."""
    # Scopes earlier tests dropped are counted here, not below.
    _collected_after_churn(state)
    before = _collected_after_churn(state)
    made = _scope_made_elsewhere(state)
    assert _collected_after_churn(state) == before
    assert made[0].eval_expr("x.a").integer() == 42

    made.clear()
    assert _collected_after_churn(state) > before, "the control never fired"


async def test_a_scope_serves_over_the_wire(client: Any) -> None:
    state = await client.acquire("EvalState", await client.acquire("Store", "dummy://"))
    repl = await state.repl()
    assert await repl.process_line('a = { b = 1; }') is None
    answer = await repl.process_line("a.b + 1")
    assert await answer.integer() == 2
    selected = await repl.select("a.b")
    assert await selected.name() == "b"
    assert await (await selected.attrs()).has("b")
    await state.aclose()
