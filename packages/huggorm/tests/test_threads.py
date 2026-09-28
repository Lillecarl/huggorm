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
