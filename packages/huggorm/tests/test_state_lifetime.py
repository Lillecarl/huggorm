"""A dropped EvalState frees what it allocated outside the collector.

Nix allocates every builtin with `new PrimOp`, and only a collector-owned
Value points at it, so before nix-evalstate-owns-primops.patch each state
leaked 84.5 KiB of malloc memory when it was dropped (huggorm#162).

In a child: the measurement is process-wide, and a test beside it would
allocate into it.
"""

import subprocess
import sys

from nixversion import needs_collector

CHILD = """
import ctypes, gc
import huggorm_bindings as h
from huggorm_bindings import EvalState, Store

class MallInfo(ctypes.Structure):
    _fields_ = [(n, ctypes.c_size_t) for n in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd",
        "usmblks", "fsmblks", "uordblks", "fordblks", "keepcost")]

libc = ctypes.CDLL("libc.so.6")
libc.mallinfo2.restype = MallInfo
store = Store("dummy://")

def cycle():
    state = EvalState(store)
    state.make_primop("f", 1, lambda x: x)
    del state
    gc.collect()
    h.collect_garbage()

for _ in range(20):
    cycle()
before = libc.mallinfo2().uordblks
for _ in range(50):
    cycle()
print((libc.mallinfo2().uordblks - before) // 50)
"""


@needs_collector
def test_a_dropped_state_frees_its_primops() -> None:
    done = subprocess.run([sys.executable, "-c", CHILD],
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    per_state = int(done.stdout.split()[-1])
    # 84.5 KiB before the patch. What is left is allocator noise.
    assert per_state < 8192, f"{per_state} bytes kept per dropped state"
