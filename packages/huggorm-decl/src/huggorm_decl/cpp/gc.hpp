#pragma once

/**
 * The collector, and the threads Python made.
 *
 * Split out of `eval.hpp` with the log tap. Boehm has to be told about
 * a thread before that thread touches GC memory, and Python's executor
 * threads are invisible to its pthread interception - so every one of
 * them registers here first.
 *
 * `live_roots` is OURS rather than the collector's, and it lives here
 * because it answers a question about the same subject: a root that is
 * never dropped keeps its value alive forever, and no heap counter can
 * tell that from a heap that simply grew.
 */

#include <atomic>
#include <cstddef>

#include <gc/gc.h>

namespace huggorm {

// ---- the collector, and the threads Python made -------------------

/**
 * Registers the calling thread with the collector, once.
 *
 * Executor threads are created by Python and are invisible to the
 * collector's pthread interception. Each must register before
 * touching GC memory: allocation from an unregistered thread races
 * with a collection.
 *
 * `nix::initGC()` already calls `GC_allow_register_threads()`
 * (eval-gc.cc:68), so the permission is upstream's and only the
 * per-thread half is ours.
 *
 * The flag records whether WE registered this thread.
 * `GC_register_my_thread` answers `GC_DUPLICATE` for a thread the
 * collector already knows - the main thread, or one it created - and
 * unregistering such a thread is not ours to do.
 */
inline bool & gc_owns_registration()
{
    static thread_local bool flag = false;
    return flag;
}

/**
 * Whether this thread has been ASKED yet, as opposed to whether we
 * own its registration.
 *
 * Two flags, not one, and the difference cost 2.25 ms per value.
 * `GC_register_my_thread` answers GC_DUPLICATE for a thread the
 * collector already knows - the main thread, every time - so a single
 * flag set only on GC_SUCCESS never latched there, and every
 * Bridge, every stage and every destructor called
 * `GC_get_stack_base` again. That call is not cheap: it reads the
 * process's own memory map to find the stack bounds.
 *
 * Measured, not reasoned: 500 `make_int` calls went from 1124 ms to
 * 0.17 ms. The allocator was never the problem - swapping
 * `nix::allocRootValue` for a hand-rolled uncollectable cell changed
 * the figure by 3 ms in 1124, which is what ruled it out.
 */
inline bool & gc_asked()
{
    static thread_local bool flag = false;
    return flag;
}

/**
 * Unregisters a thread we registered, when that thread EXITS.
 *
 * Boehm stops the world by signalling every registered thread and
 * waiting for each to answer. A thread that exits while still
 * registered never answers, and the next collection aborts the
 * PROCESS with "Signals delivery fails constantly" - no exception, no
 * traceback.
 *
 * A `thread_local` object's destructor runs at thread exit, so this
 * is the one place the rule cannot be forgotten. `gc_release_thread`
 * stays bound for a caller who wants to release early; nobody has to
 * remember it any more.
 *
 * Verified by reproducing the abort first: one pool thread made a
 * value, the pool shut down, and the next `collect_garbage()` killed
 * the process.
 */
struct ThreadExit
{
    ~ThreadExit()
    {
        if (gc_owns_registration()) {
            GC_unregister_my_thread();
            gc_owns_registration() = false;
        }
    }
};

inline void gc_register_thread()
{
    if (gc_asked())
        return;
    gc_asked() = true;
    struct GC_stack_base sb;
    GC_get_stack_base(&sb);
    // GC_SUCCESS means WE registered it, so we are the ones who must
    // unregister. GC_DUPLICATE means the collector already knew, and
    // unregistering such a thread is not ours to do.
    if (GC_register_my_thread(&sb) == GC_SUCCESS) {
        gc_owns_registration() = true;
        // Constructed on first registration, destroyed at thread exit.
        static thread_local ThreadExit at_exit;
        (void) at_exit;
    }
}

/**
 * Takes the CURRENT thread off the collector's list, as its last GC
 * action before it exits.
 *
 * Boehm stops the world by signalling every registered thread and
 * waiting for each to answer. A thread that exits while still
 * registered never answers, and the next collection aborts the
 * PROCESS with "Signals delivery fails constantly". A dedicated
 * affine executor shuts its single thread down on close, which is
 * exactly that shape.
 */
inline void gc_unregister_thread()
{
    if (!gc_owns_registration())
        return;
    GC_unregister_my_thread();
    gc_owns_registration() = false;
    gc_asked() = false;
}

inline void gc_collect()
{
    // Two cycles: finalizers and frees lag one behind.
    gc_register_thread();
    GC_gcollect();
    GC_gcollect();
}

/**
 * How many roots this process is holding right now.
 *
 * OUR bookkeeping, not the collector's, and that is the point. Heap
 * counters answer a question about boehm - which is conservative, so
 * a stale pointer in a register legitimately retains an object and a
 * byte-count assertion is flaky by design. This answers a question
 * about US: did every Bridge that was made release its root.
 *
 * A leak of roots is a real bug class and nothing else can see it: a
 * root that is never dropped keeps its value alive forever, and the
 * heap only says the heap grew.
 */
inline std::atomic<std::size_t> & live_roots()
{
    static std::atomic<std::size_t> count{0};
    return count;
}
}  // namespace huggorm
