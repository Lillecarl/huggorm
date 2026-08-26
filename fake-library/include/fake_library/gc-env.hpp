#pragma once
// GC environment for the evaluation side, mirroring Nix's eval-gc.hh.
//
// EVERY consumer must define FAKE_LIBRARY_USE_BOEHMGC=1 on the command
// line, and this header refuses to compile without it. The reason is
// ODR, not taste: gc_base appears in Value's layout, and implicit member
// functions get instantiated in every TU that includes this header. Two
// TUs that disagreed on the base would produce two incompatible Values,
// and one of them would free GC memory with plain free().
//
// The build files carry the flag: meson.build for the library,
// setup.py for the extensions.
//
// This used to AUTO-DETECT boehmgc through __has_include, with a no-GC
// fallback branch behind it. Both are gone. Detection made ODR agreement
// depend on every TU seeing the same include path - true here by luck,
// silent when it stopped being true. The fallback could never compile in
// this build graph either, because c_eval.pxd includes gc/gc.h and binds
// GC_malloc_uncollectable directly. Real Nix does support a no-GC build;
// reintroducing one here means building and testing it, not restoring a
// branch nothing ever compiled.

#include <cstddef>

#if !defined(FAKE_LIBRARY_USE_BOEHMGC)
#  error "define FAKE_LIBRARY_USE_BOEHMGC=1 when compiling any TU that includes fake_library headers"
#endif

#if FAKE_LIBRARY_USE_BOEHMGC

#  define GC_INCLUDE_NEW
#  define GC_THREADS 1

#  include <gc/gc.h>
#  include <gc/gc_cpp.h>
#  include <gc/gc_allocator.h>

namespace fake_library::gcenv {

// Base for classes whose lifetime the collector owns. Deriving puts the
// object itself into scanned memory - required whenever a class holds
// the only visible reference to other GC allocations.
using gc_base = gc;

inline void init()
{
    static bool done = [] {
        GC_INIT();
        // Permit (not perform) registration for threads created
        // outside GC knowledge.
        GC_allow_register_threads();
        return true;
    }();
    (void)done;
}

// Executor/pool threads are created by Python, invisible to the GC's
// pthread interception. Each must register before touching GC memory -
// allocation from an unregistered thread races with collection.
//
// Registration is idempotent per thread. The flag records whether WE
// registered this thread: GC_register_my_thread answers GC_DUPLICATE
// for a thread the collector already knows (the main thread, or one it
// created itself), and unregistering such a thread is not ours to do.
inline bool & owns_registration()
{
    static thread_local bool flag = false;
    return flag;
}

inline void register_current_thread()
{
    if (owns_registration())
        return;
    struct GC_stack_base sb;
    GC_get_stack_base(&sb);
    if (GC_register_my_thread(&sb) == GC_SUCCESS)
        owns_registration() = true;
}

// A thread we registered must unregister immediately before it exits.
//
// This used to say we never unregister, on the grounds that these
// threads live as long as the executor pool. That is true of the
// shared pool and false of a dedicated one: closing an affine wrapper
// shuts its single-thread executor down, and the thread died still on
// the collector's list. Boehm stops the world by signalling every
// registered thread and waiting for each to answer. A dead one never
// does, so the next collection aborted the process with "Signals
// delivery fails constantly" - including on the server, whose reaper
// closes affine wrappers on its own.
//
// Correct ONLY on the thread itself, as its last GC action.
inline void unregister_current_thread()
{
    if (!owns_registration())
        return;
    GC_unregister_my_thread();
    owns_registration() = false;
}

inline void collect()
{
    // Two cycles: finalizers and frees lag one behind.
    GC_gcollect();
    GC_gcollect();
}

}  // namespace fake_library::gcenv

#else
#  error "FAKE_LIBRARY_USE_BOEHMGC must be 1: this build has no no-GC path"
#endif
