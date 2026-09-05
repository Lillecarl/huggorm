# A leak report rides every suite run

**OPEN.** nanobind reports leaked objects at interpreter shutdown on
every run of the suite, and nothing in `tasks/` had recorded it.

## What it says

    nanobind: leaked 4 instances!
     - leaked instance ... of type "huggorm_bindings.eval.EvalState"   (x4)
    nanobind: leaked 1 types!
     - leaked type "huggorm_bindings.eval.EvalState"
    nanobind: leaked 18 functions!
     - __init__, get_store_uri, eval_expr, parse_expr, make_int,
       list_append, make_list, eval_file, subscribe_logs,
       forget_file, ... (remainder skipped)
    nanobind: this is likely caused by a reference counting issue in
    the binding code.

Always `EvalState`, always four, and the type and its functions leak
BECAUSE the instances do - nanobind cannot free a type while an
instance of it lives.

## It is not `tasks/089`

Found while adding the request id, and measured rather than assumed:
the same four instances appear with those four new tests deselected.
So it predates that work.

## What it is not yet

Unmeasured. The suite exits 0 and the report is a note at shutdown,
not a failure, so it has been passing under it. Four is a small
constant and does not grow with the test count, which points at a few
long-lived states rather than a per-test leak - but that is a guess
and this file should not carry it as a finding.

## What to do first

1. Find WHICH four. A `--deselect` bisect over the files that build
   an `EvalState` names them in a few runs.
2. Decide whether a leaked `EvalState` matters. `runtime.py`'s
   `_release_gc_thread` exists because a dedicated thread that dies
   while still registered aborts the next collection with "Collecting
   from unknown thread". A leaked state is a different shape - the
   object outliving the interpreter rather than the thread outliving
   the registration - and whether it reaches the same failure is
   exactly what is unknown here.
3. Only then, fix or record as accepted.

Opened 2026-09-05, from a suite run for `tasks/089`.
