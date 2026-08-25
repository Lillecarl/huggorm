#pragma once
// C++ that store.pyx needs and a pxd cannot say. See _cpp/README for
// what belongs in here and what does not.
//
// Two shapes recur, and both are about C++ rather than about Nix.
//
// A type with no default constructor cannot be returned BY VALUE into
// Cython: Cython declares a temporary to hold it first. nix::StorePath
// is one, so anything returning one returns a pointer from here and
// the binding owns it.
//
// A member reached through a reference member - Store::config - cannot
// be described in a pxd without declaring the whole config type, which
// is a far larger surface than the one string that is wanted.

#include <memory>
#include <string>
#include <vector>

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
 * null. Cython has no declaration for it; the implicit conversion to
 * shared_ptr does the work and keeps the store alive.
 */
inline std::shared_ptr<nix::Store> open_store(const std::string & uri)
{
    return nix::openStore(uri);
}

/**
 * There is no getUri() any more: 2.34 moved it onto the config as
 * getHumanReadableURI, and Store reaches its config by reference.
 */
inline std::string store_uri(const nix::Store & store)
{
    return store.config.getHumanReadableURI();
}

inline nix::StorePath * parse_store_path(const nix::Store & store, const std::string & path)
{
    return new nix::StorePath(store.parseStorePath(path));
}

/**
 * queryAllValidPaths answers with a StorePathSet, and the same
 * restriction applies one level down: Cython declares a temporary to
 * hold each element of a loop, and nix::StorePath cannot be declared
 * without arguments. So the elements are heap pointers, and the
 * binding takes ownership of every one.
 *
 * The catch is not error handling - the error goes back up untouched.
 * It is the ownership a vector of raw pointers cannot express: what is
 * already allocated has to go back if the next allocation throws.
 */
inline std::vector<nix::StorePath *> query_all_valid_paths(nix::Store & store)
{
    std::vector<nix::StorePath *> out;
    try {
        for (auto & path : store.queryAllValidPaths())
            out.push_back(new nix::StorePath(path));
    } catch (...) {
        for (auto * path : out)
            delete path;
        throw;
    }
    return out;
}

}  // namespace cythonix
