"""Which threads huggorm starts, and when.

In a fresh interpreter each time, because this process already holds
an evaluator and so already runs the collector's markers.
"""

import subprocess
import sys

PROBE = """
import os

def threads():
    return len(os.listdir("/proc/self/task"))

import huggorm_bindings as h
print(threads())
store = h.Store("dummy://")
print(threads())
state = h.EvalState(store)
print(threads())
"""


def test_nothing_runs_until_there_is_an_evaluator() -> None:
    """Import and a store start no thread; the evaluator starts the
    collector's markers.

    `unshare(CLONE_NEWUSER)` refuses a process with more than one
    thread, so a caller that imports huggorm before it enters a
    namespace needs the import to start none."""
    out = subprocess.run([sys.executable, "-c", PROBE], capture_output=True,
                         text=True, check=True)
    imported, opened, evaluating = map(int, out.stdout.split())
    assert (imported, opened) == (1, 1)
    assert evaluating > 1


START = """
import os
import huggorm_bindings as h
h.start_collector()
print(len(os.listdir("/proc/self/task")))
"""


def test_a_caller_can_start_the_collector_before_an_evaluator() -> None:
    """For a caller that reads `nix-path` first: `start_collector` is
    where Nix copies `NIX_PATH` into it."""
    out = subprocess.run([sys.executable, "-c", START], capture_output=True,
                         text=True, check=True)
    assert int(out.stdout) > 1


def test_the_collector_s_owner_is_the_importing_thread() -> None:
    """The thread Boehm treats as its main thread, and it must outlive
    every other. pytest imported huggorm on its main thread."""
    import threading

    from huggorm_bindings import collector_owner_thread

    assert collector_owner_thread() == threading.main_thread().native_id


def test_a_held_root_shows_in_the_uncollectable_bytes() -> None:
    """Every root is one uncollectable block, so `non_gc_bytes` moves
    with the roots a caller holds and falls back when it drops them."""
    import gc

    from huggorm_bindings import EvalState, Store, collect_garbage, gc_stats

    state = EvalState(Store("dummy://"))
    collect_garbage()
    before = gc_stats()["non_gc_bytes"]
    held = [state.eval_expr(str(i)) for i in range(500)]
    collect_garbage()
    holding = gc_stats()["non_gc_bytes"]
    del held
    gc.collect()
    collect_garbage()
    after = gc_stats()["non_gc_bytes"]
    assert holding - before > 500 * 16
    assert after - before < (holding - before) // 10
