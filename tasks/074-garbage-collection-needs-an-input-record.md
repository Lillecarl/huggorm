# Garbage collection needs a record a CALLER builds

**OPEN.** `GCAction` is the last vocabulary in `tasks/070`'s queue
that has a binding waiting for it, and the binding is not a method
with a few parameters. Upstream:

    virtual void collectGarbage(const GCOptions & options,
                                GCResults & results) = 0;

Two records, and only one of them is a shape this repo has.

## What the DSL does not have yet

`GCResults` is easy: two fields, and it is something libstore MADE,
which is exactly `@produced(by=...)`.

    struct GCResults {
        StringSet paths;      // roots, or what was/would be deleted
        uint64_t bytesFreed;
    };

`GCOptions` is the new thing. A caller BUILDS one:

    struct GCOptions {
        GCAction action{gcDeleteDead};
        bool ignoreLiveness{false};
        StorePathSet pathsToDelete;   // for gcDeleteSpecific
        uint64_t maxFreed{max};
    };

Every record in this repo today is produced. `PathInfo`,
`KeyedBuildResult`, `Realisation` - libstore made each one and Python
reads it. Nothing yet is a struct Python fills in and hands DOWN.

## The two answers, and the first is not obviously wrong

**Flatten it into keyword parameters.**

    def collect_garbage(self, action: GCAction = GCAction.DELETE_DEAD,
                        ignore_liveness: Bint = False,
                        paths_to_delete: "list[StorePath]" = None,
                        max_freed: U64 = ...) -> "GCResults":

The DSL needs nothing new: these are four parameters of types it
already spells, and the `Cxx` body fills the struct. It reads well
from Python - `store.collect_garbage(max_freed=1 << 30)` - and the
struct never reaches a caller.

Against it: `max_freed`'s default is
`std::numeric_limits<uint64_t>::max()`, which has no Python literal a
`default_source` would accept, so the default has to be spelled some
other way. And every future options struct repeats the flattening by
hand in a `Cxx` body, which is the mapping this repo exists to
derive.

**Teach the declaration an INPUT record.** A class whose fields
Python sets and whose C++ the emitter fills, the mirror of
`@produced`. More machinery, and it earns itself the second time an
options struct appears - `BuildOptions`, `SubstitutablePathInfo`'s
callers, and the fetch settings are candidates.

Do not decide this from GC alone. Count the input structs upstream
actually has before building the general answer.

## The name clash, which is not cosmetic

`huggorm.collect_garbage` already EXISTS and means something else. It
is the boehm collector for the evaluator - `decl/eval.py`, a free
function over `huggorm::gc_collect` - and it collects Values, not
store paths.

A `Store.collect_garbage` beside it is two unrelated collectors one
import apart. A caller who reads "collect_garbage" in a traceback
will not know which. Options: name the store one after what upstream
calls the operation (`nix-store --gc`, so `gc` or `delete_garbage`),
or rename the evaluator's to say whose heap it sweeps
(`collect_value_garbage`). The second is a rename of a published
name, so it is Carl's call.

## Why it is worth doing

`nix-collect-garbage` is one of the two things a person does to a
store that this binding cannot do at all (the other is
`Store.repair_path`). It is also the only remaining USER of a queued
vocabulary, so `tasks/070` cannot finish without it.

## What is NOT done

All of it. Opened 2026-09-02 while landing `TrustedFlag`, because the
queue in 070 said "GCAction" as if it were one more vocabulary and it
is not.
