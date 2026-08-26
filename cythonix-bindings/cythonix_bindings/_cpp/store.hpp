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

#include <filesystem>
#include <memory>
#include <string>
#include <vector>

#include "nix/store/globals.hh"
#include "nix/store/local-fs-store.hh"
#include "nix/store/store-api.hh"
#include "nix/store/path-info.hh"
#include "nix/store/store-open.hh"
#include "nix/util/file-content-address.hh"
#include "nix/util/hash.hh"
#include "nix/util/posix-source-accessor.hh"
#include "nix/util/serialise.hh"
#include "nix/util/source-path.hh"

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

/**
 * Which store path CONTAINS this file, and where inside it.
 *
 * A different question from parse_store_path, which takes the store
 * path itself and nothing below it. `/nix/store/<hash>-python/bin/
 * python3` is not a store path; it is a file in one, and this is the
 * call that says which.
 *
 * String work only. It splits on the store DIRECTORY and never touches
 * the filesystem, so it answers for a path that does not exist and for
 * a store whose files are somewhere else - which is every chroot
 * store. Symlinks are not followed either; nix::Store has
 * followLinksToStorePath for that and it is a different binding.
 *
 * A pair, because that is what upstream returns and both halves are
 * the answer: the store path locates the object, the sub-path locates
 * the file within it. The sub-path is empty when the path IS the store
 * path, which is absOrEmpty's own meaning.
 */
struct StoreLocationParts
{
    std::string path;
    std::string sub_path;
};

inline StoreLocationParts to_store_path(const nix::Store & store, const std::string & path)
{
    auto [store_path, sub] = store.config.toStorePath(path);
    return StoreLocationParts{
        std::string(store_path.to_string()),
        sub.absOrEmpty(),
    };
}

/**
 * Follow symlinks until the path lands in the store, and stop there.
 *
 * The first half of follow_links_to_store_path, and the half that
 * keeps what the other one drops: the sub-path. A `result` symlink
 * pointing at a package resolves to <store path>/bin/foo, not to the
 * store path.
 *
 * The answer is in the STORE's terms, like print_store_path - its
 * directory is the store directory, which a chroot store keeps at
 * /nix/store while its files live somewhere else. So this is a string
 * and not a location on this machine; real_path is that.
 */
inline std::string follow_links_to_store(const nix::Store & store, const std::string & path)
{
    return store.followLinksToStore(path).string();
}

/**
 * The same question as to_store_path, asked of a symlink.
 *
 * `to_store_path` is string work and never reads the filesystem, so it
 * cannot answer for `/run/current-system` or for a `result` symlink -
 * neither is in the store, and both point at something that is. This
 * one follows links until it lands in the store, then splits.
 *
 * Upstream keeps only the store path and drops the sub-path, so this
 * does too. A pointer for the usual reason: nix::StorePath is not
 * default-constructible.
 */
inline nix::StorePath * follow_links_to_store_path(const nix::Store & store, const std::string & path)
{
    return new nix::StorePath(store.followLinksToStorePath(path));
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

/**
 * Base names to the set libstore takes.
 *
 * A pxd can declare a vector and cannot declare a std::set, so the
 * crossing point is a vector - the same flattening path_info does in
 * the other direction. nix::StorePath's own constructor parses each
 * name, so a malformed one raises here rather than reaching the
 * store.
 */
inline nix::StorePathSet store_path_set(const std::vector<std::string> & names)
{
    nix::StorePathSet out;
    for (auto & name : names)
        out.insert(nix::StorePath(name));
    return out;
}

inline nix::StorePath * add_to_store(
    nix::Store & store,
    const std::string & name,
    const std::string & data,
    const std::string & method,
    const std::string & hash_algo,
    const std::vector<std::string> & references)
{
    // An lvalue, so the string_view inside cannot dangle - which is
    // the case StringSource deletes its rvalue constructor to stop.
    nix::StringSource dump{data};
    return new nix::StorePath(store.addToStoreFromDump(
        dump,
        name,
        nix::FileSerialisationMethod::Flat,
        nix::ContentAddressMethod::parse(method),
        nix::parseHashAlgo(hash_algo),
        store_path_set(references)));
}

/**
 * Add a FILE OR DIRECTORY from the filesystem to the store.
 *
 * The other overload of addToStore, and the one `nix-store --add`
 * uses. It takes a nix::SourcePath, which is an accessor plus a path
 * inside it - the abstraction that lets Nix read a source out of a
 * tarball or a git tree as easily as off the disk. Cython has no
 * declaration for either half, and neither has a default constructor,
 * so the path arrives here as a string and the accessor is built here.
 *
 * weakly_canonical, and upstream asks for it: createAtRoot does NOT
 * canonicalise, deliberately, because it cannot know whether a caller
 * wants a symlink resolved. Weak canonicalisation is the minimum for
 * the SourcePath to reach the file at all, and it does not require the
 * path to exist - libstore gives the error for a missing one, which is
 * a better error than std::filesystem would.
 */
inline nix::StorePath * add_path_to_store(
    nix::Store & store,
    const std::string & name,
    const std::string & path,
    const std::string & method,
    const std::string & hash_algo,
    const std::vector<std::string> & references)
{
    auto source = nix::PosixSourceAccessor::createAtRoot(
        std::filesystem::weakly_canonical(std::filesystem::path{path}));
    return new nix::StorePath(store.addToStore(
        name,
        source,
        nix::ContentAddressMethod::parse(method),
        nix::parseHashAlgo(hash_algo),
        store_path_set(references)));
}

/**
 * Where a store object's files really are on this filesystem.
 *
 * printStorePath answers with the store DIRECTORY joined onto the
 * path, which is not the same question. A chroot store keeps
 * /nix/store as its store directory and puts the files under
 * <root>/nix/store, so its printed path does not exist.
 *
 * toRealPath is on LocalFSStore, not on Store, and upstream is right
 * about that: a binary cache or an ssh-ng store has no directory here
 * at all. So this asks whether the store IS one, and throws the same
 * exception nix::Store throws for a method it cannot answer - the
 * message shape included, because it is the same kind of answer.
 */
/**
 * What a store knows about one path it holds, flattened.
 *
 * nix::ValidPathInfo is not default-constructible, holds a nix::Hash
 * and a std::optional<StorePath>, and arrives behind a ref<const T>.
 * Cython can declare none of those. So the crossing point is a POD of
 * already-converted fields: it default-constructs, every member has a
 * pxd spelling, and the conversions happen once, here, next to the
 * types they convert.
 *
 * That is a translation rather than a thin binding, and it is the
 * honest place for one. The alternative is a pointer-owning wrapper
 * over ValidPathInfo plus a from-parts constructor in C++, which is
 * more machinery for a struct nobody mutates.
 *
 * An absent deriver is the empty string. Nix has no store path whose
 * base name is empty - parseStorePath refuses one - so the two cannot
 * be confused, and the binding turns it back into None.
 *
 * Nix32, because that is what `nix path-info` and a .narinfo print:
 * `sha256:<base32>`. The algorithm travels with the digest, so a
 * caller never has to be told separately which one it is.
 */
struct PathInfoParts
{
    std::string path;
    std::string nar_hash;
    uint64_t nar_size;
    std::string deriver;
    int64_t registration_time;
    bool ultimate;
    // Nix keeps both as SETS. A vector because that is what a pxd can
    // declare and what a repeated protobuf field is; the order is the
    // set's own, which is sorted, so it is stable between calls.
    std::vector<std::string> references;
    std::vector<std::string> sigs;
};

inline PathInfoParts path_info(nix::Store & store, const nix::StorePath & path)
{
    auto info = store.queryPathInfo(path);
    PathInfoParts out{
        std::string(info->path.to_string()),
        info->narHash.to_string(nix::HashFormat::Nix32, /*includeAlgo=*/true),
        info->narSize,
        info->deriver ? std::string(info->deriver->to_string()) : std::string(),
        static_cast<int64_t>(info->registrationTime),
        info->ultimate,
        {},
        {},
    };
    // Base names, the same spelling the `path` field uses. A store
    // path is a name and not a location, so printing one here would
    // pick a store directory that this shim has no business choosing.
    for (auto & ref : info->references)
        out.references.push_back(std::string(ref.to_string()));
    // nix::Signature is a key name and raw bytes, not a string. Its
    // own to_string is the `<key-name>:<base64>` spelling every Nix
    // tool prints and parses, so the binding does not invent one.
    for (auto & sig : info->sigs)
        out.sigs.push_back(sig.to_string());
    return out;
}

inline std::string real_path(nix::Store & store, const nix::StorePath & path)
{
    auto * fs = dynamic_cast<nix::LocalFSStore *>(&store);
    if (fs == nullptr)
        throw nix::Unsupported(
            "operation 'real_path' is not supported by store '%s'",
            store.config.getHumanReadableURI());
    return fs->toRealPath(path).string();
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
