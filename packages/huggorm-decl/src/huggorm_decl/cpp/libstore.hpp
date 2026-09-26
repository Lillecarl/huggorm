#pragma once

/**
 * The one call every libstore binding needs.
 *
 * `errors.hpp` is the other file in this directory, and it is here
 * for the same reason: it is C++ a DECLARATION names rather than C++
 * a binding derives. `decl/path.py` points `@binds` at
 * `huggorm::init_libstore`, and the emitter writes the `m.def`.
 *
 * `open_store` was the other half and is gone. It wrapped
 * `nix::openStore` in three lines to take the result as a
 * shared_ptr, which is a CONVERSION and so a mapping. It was here
 * only because a factory had to be a named C++ symbol; the emitter
 * writes the lambda now, and `decl/store.py` carries the one call
 * (tasks/063).
 */

// initLibStore lives in globals.hh.
#include "nix/store/globals.hh"

namespace huggorm {

/**
 * libstore has to be initialised before anything else in it is
 * called, and it does not raise when it has not been: it ABORTS the
 * process, with "The program must call nix::initNix() before calling
 * any libstore library functions". A binding cannot let a caller
 * discover that.
 *
 * initLibStore, not initNix: initNix lives in libnixmain and does the
 * things a COMMAND needs - argv0, signal handlers, a logger writing to
 * stderr. A library embedded in someone else's process should not take
 * those over. initLibStore also calls initLibUtil for us.
 *
 * Idempotent, and called from the module's own initialisation, so
 * every path into libstore is behind it.
 *
 * `false`: nix.conf and NIX_CONFIG are not read here. A library does
 * not take the host's configuration because it was imported; a
 * caller that wants it calls `load_config` (decl/eval.py).
 */
inline void init_libstore()
{
    static bool done = [] {
        nix::initLibStore(false);
        return true;
    }();
    (void) done;
}

}  // namespace huggorm
