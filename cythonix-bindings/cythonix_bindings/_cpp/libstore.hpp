#pragma once

/**
 * The two calls every libstore binding needs, in either backend.
 *
 * `errors.hpp` is the other file in this directory, and it is here
 * for the same reason: it is C++ that neither backend owns. Cython
 * reaches these through a `cdef extern` block and nanobind through a
 * `m.def`, and what they do is the same either way.
 */

#include <memory>
#include <string>

// initLibStore lives in globals.hh, openStore in store-open.hh.
#include "nix/store/globals.hh"
#include "nix/store/store-api.hh"
#include "nix/store/store-open.hh"

namespace cythonix {

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
 */
inline void init_libstore()
{
    static bool done = [] {
        nix::initLibStore();
        return true;
    }();
    (void) done;
}

/**
 * nix::openStore returns a ref<Store>, a shared_ptr that cannot be
 * null. Neither backend has a declaration for that type; the implicit
 * conversion to shared_ptr does the work and keeps the store alive
 * for as long as Python holds one.
 */
inline std::shared_ptr<nix::Store> open_store(const std::string & uri)
{
    return nix::openStore(uri);
}

}  // namespace cythonix
