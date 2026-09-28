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
#include <cstdint>
#include <mutex>

#include <unistd.h>

#include <gc/gc.h>

#include "nix/expr/eval-gc.hh"

namespace huggorm {

// ---- the collector's start -----------------------------------------

/** The thread `gc_boot` ran on, as the kernel numbers it, or 0. */
inline std::atomic<std::int64_t> & gc_owner()
{
    static std::atomic<std::int64_t> tid{0};
    return tid;
}

/**
 * Boehm's own start, at import, on the importing thread. It starts no
 * thread.
 *
 * The thread that calls `GC_init` becomes the collector's main thread,
 * and its stack is scanned up to the PROCESS's main stack base
 * (`pthread_stop_world.c:849`). So it has to run on the thread
 * that imports, and cannot wait for a pool thread.
 *
 * The marker threads, 15 on dynhetz, start in
 * `GC_allow_register_threads` (`pthread_support.c:2150`), which
 * `nix::initGC` calls. That waits for `gc_start`. A process that only
 * imports, or only opens a store, runs no thread of the collector:
 * `unshare(CLONE_NEWUSER)` refuses a process with more than one.
 *
 * The two settings `nix::initGC` makes before its own `GC_INIT`
 * (eval-gc.cc:55-59) come first here, so `GC_init` runs in the mode
 * Nix asks for. bdwgc calls a later switch "not recommended"
 * (misc.c:2573), and its setter drops offset 0 without huggorm's
 * patch, `nix/patches/bdwgc-late-interior-pointers.patch`.
 */
inline void gc_boot()
{
    GC_set_all_interior_pointers(0);
    GC_set_no_dls(1);
    GC_INIT();
#ifdef __linux__
    gc_owner().store(::gettid(), std::memory_order_release);
#endif
}

/**
 * `nix::initGC`, once, before the first GC allocation or thread
 * registration.
 *
 * `nix::initGC` guards itself with a plain bool, so two threads could
 * both pass its test; `call_once` serialises them. It repeats the two
 * settings `gc_boot` made, to the same values.
 *
 * It also copies `NIX_PATH` into `nix-path`, so that happens when the
 * first evaluator is made, after nix.conf is loaded. The Nix CLI has
 * the same order: the environment wins over nix.conf.
 */
inline void gc_start()
{
    static std::once_flag once;
    std::call_once(once, [] { nix::initGC(); });
}

// ---- the collector, and the threads Python made -------------------

/**
 * Registers the calling thread with the collector, once.
 *
 * Executor threads are created by Python and are invisible to the
 * collector's pthread interception. Each must register before
 * touching GC memory: allocation from an unregistered thread races
 * with a collection.
 *
 * `nix::initGC()` calls `GC_allow_register_threads()` (eval-gc.cc:68),
 * so the permission is upstream's and only the per-thread half is
 * ours. `gc_start` runs it first.
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
    gc_start();
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

/**
 * How many REPL environments the collector has freed.
 *
 * The one way to see that a scope's root holds. A freed block keeps
 * its contents until the collector hands it out again, so reading a
 * binding back succeeds whether the environment is alive or not. A
 * finalizer runs when the collector finds the block unreachable.
 *
 * `no_order`, because Boehm refuses to finalize a block in a cycle of
 * finalizable blocks, and an environment refers to itself through its
 * own thunks.
 */
inline std::atomic<std::size_t> & scopes_collected()
{
    static std::atomic<std::size_t> count{0};
    return count;
}

inline void count_when_collected(void * block)
{
    GC_register_finalizer_no_order(
        block,
        [](void *, void *) { scopes_collected().fetch_add(1, std::memory_order_relaxed); },
        nullptr, nullptr, nullptr);
}
}  // namespace huggorm
