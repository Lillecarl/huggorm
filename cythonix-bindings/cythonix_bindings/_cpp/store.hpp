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
#include "nix/util/file-content-address.hh"
#include "nix/util/hash.hh"
#include "nix/util/serialise.hh"

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
/**
 * Add one file's CONTENTS to the store.
 *
 * addToStoreFromDump takes a Source, which is an interface Cython has
 * no declaration for, and four enums it has no spelling for either.
 * The enums arrive here as the strings Nix itself parses - `flat`,
 * `nar`, `git`, `text` for the method, `sha256` and friends for the
 * algorithm - so the vocabulary stays Nix's and so does the error
 * when a caller invents one.
 *
 * The dump is FLAT and this does not ask. `data` is the contents of a
 * regular file, so that is the only serialisation it can be; a NAR
 * would be a different argument with a different meaning. Nix
 * enforces the rest - a hash method whose ingestion is not flat is
 * refused, by libstore, with libstore's own message.
 */
inline nix::StorePath * add_to_store(
    nix::Store & store,
    const std::string & name,
    const std::string & data,
    const std::string & method,
    const std::string & hash_algo)
{
    // An lvalue, so the string_view inside cannot dangle - which is
    // the case StringSource deletes its rvalue constructor to stop.
    nix::StringSource dump{data};
    return new nix::StorePath(store.addToStoreFromDump(
        dump,
        name,
        nix::FileSerialisationMethod::Flat,
        nix::ContentAddressMethod::parse(method),
        nix::parseHashAlgo(hash_algo)));
}

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
