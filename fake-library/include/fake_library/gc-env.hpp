#pragma once
// GC environment for the evaluation side, mirroring Nix's eval-gc.hh:
// a compile-time switch with dummy aliases so the rest of the code has
// no #ifdefs.
//
// The switch AUTO-DETECTS boehmgc via __has_include. This is not a
// convenience: the allocator alias appears in EvalState's layout, and
// implicit member functions get instantiated in EVERY TU that includes
// this header. If TUs disagreed on the alias, one would free GC-allocated
// arena nodes with plain free(). Every consumer of this header therefore
// needs gc/gc.h on its include path (and links libgc).

#include <cstddef>

#if !defined(FAKE_LIBRARY_USE_BOEHMGC)
#  if defined(__has_include)
#    if __has_include(<gc/gc.h>)
#      define FAKE_LIBRARY_USE_BOEHMGC 1
#    endif
#  endif
#endif

#ifndef FAKE_LIBRARY_USE_BOEHMGC
#  define FAKE_LIBRARY_USE_BOEHMGC 0
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
// Registration is idempotent per thread. We never unregister: these
// threads live as long as the executor pool, and unregistering is only
// correct immediately before thread exit, which we do not control.
inline void register_current_thread()
{
    static thread_local int registered = [] {
        struct GC_stack_base sb;
        GC_get_stack_base(&sb);
        GC_register_my_thread(&sb);
        return 0;
    }();
    (void)registered;
}

inline void collect()
{
    GC_gcollect();
    GC_gcollect();  // finalizers and frees lag one cycle behind
}

}  // namespace fake_library::gcenv

#else

#  include <memory>

template<typename T>
using gc_allocator = std::allocator<T>;

// Mirrors the enabled branch's `using gc_base = gc;`: an empty base so
// consumers can derive unconditionally.
struct gc_base {};

namespace fake_library::gcenv {

inline void init() {}
inline void register_current_thread() {}
inline void collect() {}

}  // namespace fake_library::gcenv

#endif
