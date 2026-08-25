#pragma once
// A small mock of the parts of the Nix store domain that the wrapper
// experiment needs. Shapes follow src/libstore in the real tree:
//
// - StorePath: "the fundamental reference type of Nix". Immutable,
//   validated baseName of the form <32-char-base32-hash>-<name>.
//   Thread-safe: const-only access, no mutable state.
// - Derivation: mutable builder state (env map). NOT thread-safe while
//   being mutated: all access stays on one thread.
// - DerivedPath: what you ask the store to build - either an opaque
//   literal path or a derivation output. Immutable, thread-safe.
// - Store: abstract interface (like nix::Store). Implementations are
//   shared objects guarded by locks, hence safe from any thread;
//   connection-bound remotes are confined to their IO thread instead -
//   that split is the experiment's pool-vs-affine exemplar pair.
// - describe_store: free function going through C++ virtual dispatch,
//   used to observe which overrides are visible to C++.

#include <map>
#include <set>
#include <stdexcept>
#include <string>
#include <vector>

namespace fake_library {

class StorePath {
public:
    // Size of the hash part of store paths, in base-32 characters.
    constexpr static size_t HashLen = 32;

    // Default state is invalid; exists only as binding glue (generated
    // temporaries are declared before they are assigned).
    StorePath() = default;

    // Throws std::invalid_argument on a malformed hash or name.
    StorePath(std::string hash, std::string name);

    // Parse from "<hash>-<name>". Mirrors the real nix::StorePath
    // constructor taking a baseName; throws on malformed input.
    explicit StorePath(std::string base_name);

    std::string to_string() const;  // "<hash>-<name>"
    std::string hash() const;
    std::string name() const;

private:
    std::string hash_;
    std::string name_;
};

// Mutable builder state; NOT thread-safe. The access counter makes the
// confinement observable: reads mutate it, so cross-thread use would race.
class Derivation {
public:
    // Default state is invalid; exists only as binding glue.
    Derivation() = default;

    explicit Derivation(std::string name);

    void set_env(std::string key, std::string value);
    // Mutates: counts how often this derivation was inspected.
    std::string describe();
    int queries() const;

private:
    std::string name_;
    std::map<std::string, std::string> env_;
    int queries_ = 0;
};

// Immutable description of a build request. Thread-safe: const-only.
class DerivedPath {
public:
    // Opaque: a literal store path that must be present.
    explicit DerivedPath(StorePath path);
    // Built: the given output of a derivation in the store.
    DerivedPath(StorePath drvPath, std::string output);

    std::string describe() const;

    const StorePath & path() const;
    // Empty for opaque requests.
    const std::string & output_name() const;
    bool is_built() const;

private:
    StorePath path_;
    bool built_ = false;
    std::string output_;
};

// Abstract store interface. Concrete implementations register into a
// mutex-guarded table, so multi-threaded use is legitimate for them.
class Store {
public:
    Store();
    virtual ~Store();

    virtual std::string get_uri() const = 0;

    bool is_valid_path(const StorePath & path) const;

    // Every path this store holds. The real nix::Store answers the
    // same question with a StorePathSet; a vector is the same answer
    // in the order a repeated protobuf field keeps.
    std::vector<StorePath> query_all_valid_paths() const;

    // Deliberately slow: hashes and registers. Safe to call with the GIL
    // released. Returns the new store path.
    StorePath add_text_to_store(std::string name, std::string contents) const;

    // Deliberately slow. For an opaque request the path must already be
    // valid; for a built request it produces and registers the output.
    StorePath build_derivation(const DerivedPath & request) const;

    // Parses a .drv previously added to this store. Throws
    // std::invalid_argument when the path is unknown or not a .drv.
    Derivation query_derivation(const StorePath & drv_path) const;

protected:
    void register_(const std::string & base_name) const;
    bool lookup(const std::string & base_name) const;

private:
    mutable std::set<std::string> valid_;
    // std::mutex*, pimpl'd to keep the header light. The CONSTRUCTOR
    // creates it. Lazy creation on first use was a data race: two
    // threads adding to the same store both saw a null pointer and both
    // allocated, leaving two mutexes guarding one set.
    void * lock_ = nullptr;
};

class LocalStore : public Store {
public:
    std::string get_uri() const override;  // "local"
};

// Connection-bound store: in the real world its socket owns a thread,
// so operations are pinned to one IO thread. Affine exemplar.
class RemoteStore : public Store {
public:
    std::string get_uri() const override;  // "uds://daemon"
};

// Free function through C++ virtual dispatch: sees overrides only when
// the binding's trampoline forwards them.
std::string describe_store(const Store & store);

}  // namespace fake_library
