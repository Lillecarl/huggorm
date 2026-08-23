# GC build agreement: explicit, not luck

Review findings 9 + 10. ODR safety currently rests on __has_include
agreeing across TUs; the no-GC fallback branch is unreachable dead
code in this build graph (c_eval.pxd includes gc/gc.h
unconditionally). Also: EvalState.force and Value accessors skip
gc_register_current_thread - safe only transitively today, unsafe when
forcing a value produced by another state's runner.

Fix: pass -DFAKE_LIBRARY_USE_BOEHMGC=1 explicitly from bindings
setup.py (or #error on disagreement); register in force + Value
accessors; delete or consciously keep the fallback branch.
