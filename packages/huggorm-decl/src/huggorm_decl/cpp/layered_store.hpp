#pragma once
/**
 * A `nix::Store` that Python implements, layered over another store
 * (huggorm#149).
 *
 * `LayeredStore` is the base a Python subclass extends. Each hook has
 * the name and the contract of the `Store` method a caller already
 * knows, so a subclass overrides `Store`'s own methods, and Python and
 * Nix reach the same override. `decl/layered_store.py` marks each
 * `@virtual`, and the emitted trampoline calls the override. What a
 * declaration cannot say lives here: Nix's own virtuals are protected,
 * `noexcept`, answer through a `Callback` or fill an out-parameter, and
 * each of those calls the hook.
 *
 * A hook's C++ implementation is the layering: the same operation on
 * the underlying store. When there is none, it is `nix::Store`'s own
 * default, or `nix::Unsupported` where Nix has no default, as a cache
 * store raises for what it cannot do. An override reaches it
 * with `super()`. The trampoline calls it without the GIL, so a daemon
 * round trip below blocks no Python thread.
 *
 * Registration inserts into Nix's public `Implementations::registered()`
 * at run time, so a scheme needs no C++ template of its own.
 */

#include <map>
#include <memory>
#include <optional>
#include <string>
#include <type_traits>
#include <utility>
#include <vector>

#include <nanobind/nanobind.h>
#include <nanobind/stl/shared_ptr.h>

#include "nix/store/build-result.hh"
#include "nix/store/derivations.hh"
#include "nix/store/path-info.hh"
#include "nix/store/realisation.hh"
#include "nix/store/store-api.hh"
#include "nix/store/store-registration.hh"
#include "nix/util/callback.hh"

#include "huggorm_decl/cpp/serialise.hpp"

// Nix 2.36 moves `ensurePath` and the builds onto a `Builder` that
// `Store::getBuilder` hands out, in a header earlier Nix does not have.
#if __has_include("nix/store/build.hh")
#  include "nix/store/build.hh"
#endif

namespace huggorm {

namespace nb = nanobind;

/**
 * A strong reference to a Python object that Nix may drop on any
 * thread. It takes the GIL to release, and leaks instead once the
 * interpreter is finalizing or gone.
 */
class PyRef
{
public:
    explicit PyRef(nb::object obj)
        : obj_(obj.release().ptr())
    {
    }

    PyRef(const PyRef &) = delete;
    PyRef & operator=(const PyRef &) = delete;

    ~PyRef()
    {
        if (obj_ == nullptr || !Py_IsInitialized() || Py_IsFinalizing())
            return;
        nb::gil_scoped_acquire gil;
        Py_DECREF(obj_);
    }

    nb::handle get() const
    {
        return obj_;
    }

private:
    PyObject * obj_;
};

struct LayeredStoreConfig : std::enable_shared_from_this<LayeredStoreConfig>, virtual nix::StoreConfig
{
    std::shared_ptr<PyRef> factory;
    std::string scheme;
    std::string authority;
    /**
     * The URI parameters no Nix store setting claims. They are the
     * Python store's own, so the subclass reads them, and Nix does not
     * warn about them.
     */
    std::map<std::string, std::string> params;

    /**
     * Nix 2.35 gives `StoreConfig` a `FilePathType` argument and `Store`
     * a pure `anchor()` together. `anchor` is private and `FilePathType`
     * protected, so only a subclass can see the change, and this one
     * marks both. A template, so a Nix without it discards the branch
     * instead of failing to compile it.
     */
    template<typename Config = LayeredStoreConfig>
    static constexpr bool has_path_type = requires { typename Config::FilePathType; };

    template<typename Config = LayeredStoreConfig>
    static nix::ref<nix::StoreConfig> make(
        std::shared_ptr<PyRef> factory, std::string_view scheme, std::string_view authority, const Params & params)
    {
        // `Unix`, as the dummy store: the store directory is not a host path.
        if constexpr (has_path_type<Config>)
            return nix::make_ref<Config>(std::move(factory), scheme, authority, params, Config::FilePathType::Unix);
        else
            return nix::make_ref<Config>(std::move(factory), scheme, authority, params);
    }

    template<typename... PathType>
    LayeredStoreConfig(
        std::shared_ptr<PyRef> factory,
        std::string_view scheme,
        std::string_view authority,
        const Params & given,
        PathType... path_type)
        : StoreConfig(given, path_type...)
        , factory(std::move(factory))
        , scheme(scheme)
        , authority(authority)
    {
        // Nix's map compares with `std::less<void>`, the caster's with
        // the default, so the two are different types.
        auto own = std::exchange(unknownSettings, {});
        params.insert(own.begin(), own.end());
    }

    nix::ref<nix::Store> openStore() const override;

    nix::StoreReference getReference() const override
    {
        auto all = getQueryParams();
        all.insert(params.begin(), params.end());
        return {
            .variant = nix::StoreReference::Specified{.scheme = scheme, .authority = authority},
            .params = std::move(all),
        };
    }
};

template<typename Base>
struct Anchored : Base
{
    using Base::Base;

private:
    void anchor() override {}
};

/**
 * Nix after 2.35 makes `registerDrvOutputUnchecked` the pure virtual
 * that `registerDrvOutput(output)` was. It is protected, so only a
 * subclass sees which one this Nix has.
 */
struct Probe : nix::Store
{
    template<typename Self = Probe>
    static constexpr bool unchecked =
        requires(Self & self, const nix::Realisation & output) { self.registerDrvOutputUnchecked(output); };
};

/** The pure registration this Nix has, as the checked one without checks. */
template<typename Base>
struct Registers : Base
{
    using Base::Base;
    using Base::registerDrvOutput;

protected:
    void registerDrvOutputUnchecked(const nix::Realisation & output) override
    {
        this->registerDrvOutput(output, nix::NoCheckSigs);
    }
};

template<typename Base>
struct RegistersOne : Base
{
    using Base::Base;
    using Base::registerDrvOutput;

    void registerDrvOutput(const nix::Realisation & output) override
    {
        this->registerDrvOutput(output, nix::NoCheckSigs);
    }
};

using Anchor = std::conditional_t<LayeredStoreConfig::has_path_type<>, Anchored<nix::Store>, nix::Store>;
using StoreBase = std::conditional_t<Probe::unchecked<>, Registers<Anchor>, RegistersOne<Anchor>>;

class LayeredStore : public StoreBase
{
public:
    LayeredStore(LayeredStoreConfig & config, const std::optional<std::shared_ptr<nix::Store>> & underlying)
        : StoreBase{config}
        , config_(config.shared_from_this())
        , underlying_(underlying.value_or(nullptr))
    {
    }

    virtual ~LayeredStore() = default;

    // -- the hooks: what a Python subclass overrides ---------------------

    virtual bool is_valid_path(const nix::StorePath & path)
    {
        if (underlying_)
            return underlying_->isValidPath(path);
        return Store::isValidPathUncached(path);
    }

    virtual nix::ValidPathInfo query_path_info(const nix::StorePath & path)
    {
        return *below("queryPathInfo").queryPathInfo(path);
    }

    virtual std::optional<nix::StorePath> query_path_from_hash_part(const std::string & hash_part)
    {
        return below("queryPathFromHashPart").queryPathFromHashPart(hash_part);
    }

    virtual std::vector<nix::StorePath> query_all_valid_paths()
    {
        return listed(below("queryAllValidPaths").queryAllValidPaths());
    }

    virtual std::vector<nix::StorePath> query_referrers(const nix::StorePath & path)
    {
        nix::StorePathSet referrers;
        below("queryReferrers").queryReferrers(path, referrers);
        return listed(referrers);
    }

    virtual void add_temp_root(const nix::StorePath & path)
    {
        below("addTempRoot").addTempRoot(path);
    }

    // With nothing below, each hook from here down runs `nix::Store`'s
    // own default, which Nix builds on the hooks above: a store that
    // answers `query_path_info` answers a closure too.

    virtual std::vector<nix::StorePath> query_valid_derivers(const nix::StorePath & path)
    {
        return listed(underlying_ ? underlying_->queryValidDerivers(path) : Store::queryValidDerivers(path));
    }

    virtual std::vector<nix::StorePath> query_valid_paths(const std::vector<nix::StorePath> & paths)
    {
        nix::StorePathSet asked{paths.begin(), paths.end()};
        return listed(underlying_ ? underlying_->queryValidPaths(asked) : Store::queryValidPaths(asked));
    }

    virtual std::vector<nix::StorePath> compute_fs_closure(
        const std::vector<nix::StorePath> & paths, bool flip_direction, bool include_outputs, bool include_derivers)
    {
        nix::StorePathSet from{paths.begin(), paths.end()}, out;
        if (underlying_)
            underlying_->computeFSClosure(from, out, flip_direction, include_outputs, include_derivers);
        else
            Store::computeFSClosure(from, out, flip_direction, include_outputs, include_derivers);
        return listed(out);
    }

    virtual std::vector<nix::StorePath> query_substitutable_paths(const std::vector<nix::StorePath> & paths)
    {
        nix::StorePathSet asked{paths.begin(), paths.end()};
        return listed(underlying_ ? underlying_->querySubstitutablePaths(asked) : Store::querySubstitutablePaths(asked));
    }

    virtual nix::MissingPaths query_missing(const std::vector<nix::DerivedPath> & targets)
    {
        return underlying_ ? underlying_->queryMissing(targets) : Store::queryMissing(targets);
    }

    virtual std::optional<nix::Realisation> query_realisation(const nix::DrvOutput & id)
    {
        auto found = below("queryRealisation").queryRealisation(id);
        if (!found)
            return std::nullopt;
        return nix::Realisation{*found, id};
    }

    virtual nix::Derivation read_derivation(const nix::StorePath & path)
    {
        return underlying_ ? underlying_->readDerivation(path) : Store::readDerivation(path);
    }

    virtual void optimise_store()
    {
        if (underlying_)
            underlying_->optimiseStore();
    }

    virtual bool verify_store(bool check_contents, bool repair)
    {
        return underlying_ && underlying_->verifyStore(check_contents, repair ? nix::Repair : nix::NoRepair);
    }

    virtual void ensure_path(const nix::StorePath & path)
    {
#if __has_include("nix/store/build.hh")
        (underlying_ ? underlying_->getBuilder() : Store::getBuilder())->ensurePath(path);
#else
        if (underlying_)
            underlying_->ensurePath(path);
        else
            Store::ensurePath(path);
#endif
    }

    virtual void nar_from_path(const nix::StorePath & path, Sink & sink)
    {
        below("narFromPath").narFromPath(path, sink);
    }

    virtual void add_to_store_nar(const nix::ValidPathInfo & info, Source & source, bool repair, bool check_sigs)
    {
        below("addToStore").addToStore(
            info, source, repair ? nix::Repair : nix::NoRepair, check_sigs ? nix::CheckSigs : nix::NoCheckSigs);
    }

    // -- Nix's virtuals: each calls a hook, or forwards ------------------

    bool isValidPathUncached(const nix::StorePath & path) override
    {
        return is_valid_path(path);
    }

    void queryPathInfoUncached(
        const nix::StorePath & path, nix::Callback<std::shared_ptr<const nix::ValidPathInfo>> callback) noexcept override
    {
        try {
            callback(std::make_shared<const nix::ValidPathInfo>(query_path_info(path)));
        } catch (nix::InvalidPath &) {
            // Nix's own answer for "not here", which its cache records.
            callback(nullptr);
        } catch (...) {
            callback.rethrow();
        }
    }

    std::optional<nix::StorePath> queryPathFromHashPart(const std::string & hashPart) override
    {
        return query_path_from_hash_part(hashPart);
    }

    nix::StorePathSet queryAllValidPaths() override
    {
        auto all = query_all_valid_paths();
        return {all.begin(), all.end()};
    }

    void queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) override
    {
        auto found = query_referrers(path);
        referrers.insert(found.begin(), found.end());
    }

    void addTempRoot(const nix::StorePath & path) override
    {
        add_temp_root(path);
    }

    nix::StorePathSet queryValidDerivers(const nix::StorePath & path) override
    {
        auto found = query_valid_derivers(path);
        return {found.begin(), found.end()};
    }

    /**
     * The hook does not see `maybeSubstitute`. Nix's own default ignores
     * it too; only a daemon or an SSH store passes it on.
     */
    nix::StorePathSet queryValidPaths(const nix::StorePathSet & paths, nix::SubstituteFlag) override
    {
        auto valid = query_valid_paths({paths.begin(), paths.end()});
        return {valid.begin(), valid.end()};
    }

    void computeFSClosure(
        const nix::StorePathSet & paths,
        nix::StorePathSet & out,
        bool flipDirection,
        bool includeOutputs,
        bool includeDerivers) override
    {
        auto found = compute_fs_closure({paths.begin(), paths.end()}, flipDirection, includeOutputs, includeDerivers);
        out.insert(found.begin(), found.end());
    }

    nix::StorePathSet querySubstitutablePaths(const nix::StorePathSet & paths) override
    {
        auto found = query_substitutable_paths({paths.begin(), paths.end()});
        return {found.begin(), found.end()};
    }

    nix::MissingPaths queryMissing(const std::vector<nix::DerivedPath> & targets) override
    {
        return query_missing(targets);
    }

    void queryRealisationUncached(
        const nix::DrvOutput & id, nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback) noexcept override
    {
        try {
            auto found = query_realisation(id);
            callback(found ? std::make_shared<const nix::UnkeyedRealisation>(std::move(*found)) : nullptr);
        } catch (...) {
            callback.rethrow();
        }
    }

    nix::Derivation readDerivation(const nix::StorePath & path) override
    {
        return read_derivation(path);
    }

    void optimiseStore() override
    {
        optimise_store();
    }

    bool verifyStore(bool checkContents, nix::RepairFlag repair) override
    {
        return verify_store(checkContents, repair == nix::Repair);
    }

    // The stream a hook gets wraps Nix's, and ends with the call.
    void addToStore(
        const nix::ValidPathInfo & info,
        nix::Source & narSource,
        nix::RepairFlag repair,
        nix::CheckSigsFlag checkSigs) override
    {
        Source source{narSource};
        Ended ended{source};
        add_to_store_nar(info, source, repair == nix::Repair, checkSigs == nix::CheckSigs);
    }

    nix::StorePath addToStoreFromDump(
        nix::Source & dump,
        std::string_view name,
        nix::FileSerialisationMethod dumpMethod,
        nix::ContentAddressMethod hashMethod,
        nix::HashAlgorithm hashAlgo,
        const nix::StorePathSet & references,
        nix::RepairFlag repair) override
    {
        return below("addToStoreFromDump")
            .addToStoreFromDump(dump, name, dumpMethod, hashMethod, hashAlgo, references, repair);
    }

    using StoreBase::registerDrvOutput;

    void registerDrvOutput(const nix::Realisation & output, nix::CheckSigsFlag checkSigs) override
    {
        below("registerDrvOutput").registerDrvOutput(output, checkSigs);
    }

#if __has_include("nix/store/build.hh")
    nix::ref<nix::Builder> getBuilder(std::shared_ptr<nix::Store> evalStore) override;
#else
    void ensurePath(const nix::StorePath & path) override
    {
        ensure_path(path);
    }
#endif

    void narFromPath(const nix::StorePath & path, nix::Sink & sink) override
    {
        Sink wrapped{sink};
        Ended ended{wrapped};
        nar_from_path(path, wrapped);
    }

    nix::ref<nix::SourceAccessor> getFSAccessor(bool requireValidPath) override
    {
        return below("getFSAccessor").getFSAccessor(requireValidPath);
    }

    std::shared_ptr<nix::SourceAccessor> getFSAccessor(const nix::StorePath & path, bool requireValidPath) override
    {
        return below("getFSAccessor").getFSAccessor(path, requireValidPath);
    }

    std::optional<nix::TrustedFlag> isTrustedClient() override
    {
        // Unknown, not unsupported: Nix has an answer for "cannot say".
        return underlying_ ? underlying_->isTrustedClient() : std::nullopt;
    }

private:
    // Nix's `Store` keeps only a reference to its config.
    std::shared_ptr<LayeredStoreConfig> config_;
    std::shared_ptr<nix::Store> underlying_;

    static std::vector<nix::StorePath> listed(const nix::StorePathSet & paths)
    {
        return {paths.begin(), paths.end()};
    }

    nix::Store & below(const char * op)
    {
        if (!underlying_)
            unsupported(op);
        return *underlying_;
    }
};

#if __has_include("nix/store/build.hh")
/**
 * The builder below, with `ensurePath` handed to the store's hook. The
 * other operations have no hook yet, so they run below as they are.
 */
class LayeredBuilder : public nix::Builder
{
public:
    LayeredBuilder(std::shared_ptr<LayeredStore> store, nix::ref<nix::Builder> below)
        : store_(std::move(store))
        , below_(std::move(below))
    {
    }

    void buildPaths(const std::vector<nix::DerivedPath> & reqs, nix::BuildMode buildMode) override
    {
        below_->buildPaths(reqs, buildMode);
    }

    std::vector<nix::KeyedBuildResult>
    buildPathsWithResults(const std::vector<nix::DerivedPath> & reqs, nix::BuildMode buildMode) override
    {
        return below_->buildPathsWithResults(reqs, buildMode);
    }

    nix::BuildResult
    buildDerivation(const nix::StorePath & drvPath, const nix::BasicDerivation & drv, nix::BuildMode buildMode) override
    {
        return below_->buildDerivation(drvPath, drv, buildMode);
    }

    void ensurePath(const nix::StorePath & path) override
    {
        store_->ensure_path(path);
    }

    void repairPath(const nix::StorePath & path) override
    {
        below_->repairPath(path);
    }

private:
    std::shared_ptr<LayeredStore> store_;
    nix::ref<nix::Builder> below_;
};

inline nix::ref<nix::Builder> LayeredStore::getBuilder(std::shared_ptr<nix::Store> evalStore)
{
    auto below = underlying_ ? underlying_->getBuilder(evalStore) : Store::getBuilder(evalStore);
    return nix::make_ref<LayeredBuilder>(
        std::static_pointer_cast<LayeredStore>(shared_from_this()), std::move(below));
}
#endif

inline nix::ref<nix::Store> LayeredStoreConfig::openStore() const
{
    nb::gil_scoped_acquire gil;
    auto self = std::const_pointer_cast<LayeredStoreConfig>(shared_from_this());
    nb::object made = factory->get()(self);
    std::shared_ptr<LayeredStore> store;
    if (!nb::try_cast(made, store) || !store)
        throw nix::Error(
            "the '%s' store factory answered %s, not a LayeredStore", scheme, nb::type_name(made.type()).c_str());
    // Nix calls `shared_from_this()` on a store, and a `bad_weak_ptr`
    // from deep inside libstore says nothing. Ask once, here.
    try {
        (void) store->shared_from_this();
    } catch (std::bad_weak_ptr &) {
        throw nix::Error("the '%s' store cannot be shared: nanobind did not adopt it", scheme);
    }
    return nix::ref<nix::Store>(std::move(store));
}

/**
 * Make `schemes` open a store that `factory` makes, for the rest of the
 * process.
 *
 * Nix's registry is a static, destroyed after Python. So the entries
 * leave it at `atexit`, while the interpreter can still release each
 * factory and what it holds; nanobind reports every instance a factory
 * closes over as a leak otherwise.
 */
inline void register_layered_store(const std::string & name, const std::vector<std::string> & schemes, nb::object factory)
{
    static std::vector<std::string> ours = [] {
        nb::module_::import_("atexit").attr("register")(nb::cpp_function([] {
            for (auto & n : ours)
                nix::Implementations::registered().erase(n);
            ours.clear();
        }));
        return std::vector<std::string>{};
    }();
    auto held = std::make_shared<PyRef>(std::move(factory));
    nix::StoreFactory made{
        .doc = "A store implemented in Python, over another store.",
        .uriSchemes = {schemes.begin(), schemes.end()},
        .experimentalFeature = std::nullopt,
        .parseConfig = [held](std::string_view scheme, std::string_view authority, const nix::StoreConfig::Params & params)
            -> nix::ref<nix::StoreConfig> { return LayeredStoreConfig::make(held, scheme, authority, params); },
        .getConfig = [held]() -> nix::ref<nix::StoreConfig> {
            return LayeredStoreConfig::make(held, "", "", nix::StoreConfig::Params{});
        },
    };
    if (!nix::Implementations::registered().emplace(name, std::move(made)).second)
        throw nix::Error("a store named '%s' is already registered", name);
    ours.push_back(name);
}

} // namespace huggorm
