#pragma once
/**
 * A `nix::Store` implemented in Python, layered over another store
 * (huggorm#149).
 *
 * A helper, not generated: a declaration cannot implement a virtual
 * (huggorm#84, not planned), and nothing here is a Python name meaning
 * a C++ call - it is C++ calling Python. `register_store_implementation`
 * in `decl/store.py` binds the one entry point.
 *
 * The rule every operation follows, in order:
 *
 *   1. the Python store's hook, when its class defines one and it does
 *      not answer `NotImplemented`;
 *   2. the same operation on the underlying store, when there is one;
 *   3. `nix::Unsupported`, as a cache store raises for what it cannot do.
 *
 * A hook that raises is an error, never a fall-through.
 *
 * Nix calls a store from any thread. Every call into Python takes the
 * GIL, and the GIL is never held across a call on the underlying store:
 * that could be a daemon round trip, or call back into Python.
 */

#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

#include <nanobind/nanobind.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/shared_ptr.h>
#include <nanobind/stl/string.h>

#include "nix/store/path-info.hh"
#include "nix/store/store-api.hh"
#include "nix/store/store-registration.hh"
#include "nix/util/callback.hh"

namespace huggorm {

namespace nb = nanobind;

/**
 * A strong reference to a Python object that Nix may drop on any
 * thread. It takes the GIL to release, and leaks instead once the
 * interpreter is finalizing or gone: a factory lives in Nix's static
 * registry, which is destroyed after Python is.
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

struct PyStoreConfig : std::enable_shared_from_this<PyStoreConfig>, virtual nix::StoreConfig
{
    std::shared_ptr<PyRef> factory;
    std::string scheme;
    std::string authority;
    /**
     * The URI parameters no Nix store setting claims. They are the
     * Python store's own, so they go to its factory, and Nix does not
     * warn about them.
     */
    nix::StringMap own;

    PyStoreConfig(
        std::shared_ptr<PyRef> factory, std::string_view scheme, std::string_view authority, const Params & params)
        : StoreConfig(params)
        , factory(std::move(factory))
        , scheme(scheme)
        , authority(authority)
        , own(std::exchange(unknownSettings, {}))
    {
    }

    nix::ref<nix::Store> openStore() const override;

    nix::StoreReference getReference() const override
    {
        auto params = getQueryParams();
        params.insert(own.begin(), own.end());
        return {
            .variant = nix::StoreReference::Specified{.scheme = scheme, .authority = authority},
            .params = std::move(params),
        };
    }
};

class PyStore : public nix::Store
{
public:
    PyStore(nix::ref<const PyStoreConfig> config, nb::object impl, std::shared_ptr<nix::Store> underlying)
        : Store{*config}
        , config_(std::move(config))
        , underlying_(std::move(underlying))
        , is_valid_path_(hook(impl, "is_valid_path"))
        , query_path_info_(hook(impl, "query_path_info"))
        , query_path_from_hash_part_(hook(impl, "query_path_from_hash_part"))
        , query_all_valid_paths_(hook(impl, "query_all_valid_paths"))
        , query_referrers_(hook(impl, "query_referrers"))
        , add_temp_root_(hook(impl, "add_temp_root"))
        , impl_(std::make_shared<PyRef>(std::move(impl)))
    {
    }

    bool isValidPathUncached(const nix::StorePath & path) override
    {
        if (auto r = ask(is_valid_path_, [](nb::handle h) { return nb::cast<bool>(h); }, path))
            return *r;
        if (underlying_)
            return underlying_->isValidPath(path);
        return Store::isValidPathUncached(path);
    }

    void queryPathInfoUncached(
        const nix::StorePath & path, nix::Callback<std::shared_ptr<const nix::ValidPathInfo>> callback) noexcept override
    {
        try {
            auto r = ask(
                query_path_info_,
                [](nb::handle h) -> std::shared_ptr<const nix::ValidPathInfo> {
                    if (h.is_none())
                        return nullptr;
                    return std::make_shared<const nix::ValidPathInfo>(nb::cast<nix::ValidPathInfo>(h));
                },
                path);
            if (r)
                return callback(std::move(*r));
            if (!underlying_)
                unsupported("queryPathInfo");
            try {
                callback(underlying_->queryPathInfo(path).get_ptr());
            } catch (nix::InvalidPath &) {
                callback(nullptr);
            }
        } catch (...) {
            callback.rethrow();
        }
    }

    void queryRealisationUncached(
        const nix::DrvOutput & id, nix::Callback<std::shared_ptr<const nix::UnkeyedRealisation>> callback) noexcept override
    {
        try {
            if (!underlying_)
                unsupported("queryRealisation");
            callback(underlying_->queryRealisation(id));
        } catch (...) {
            callback.rethrow();
        }
    }

    std::optional<nix::StorePath> queryPathFromHashPart(const std::string & hashPart) override
    {
        if (auto r = ask(
                query_path_from_hash_part_,
                [](nb::handle h) { return nb::cast<std::optional<nix::StorePath>>(h); },
                hashPart))
            return *r;
        if (!underlying_)
            unsupported("queryPathFromHashPart");
        return underlying_->queryPathFromHashPart(hashPart);
    }

    nix::StorePathSet queryAllValidPaths() override
    {
        if (auto r = ask(query_all_valid_paths_, paths))
            return *r;
        if (!underlying_)
            unsupported("queryAllValidPaths");
        return underlying_->queryAllValidPaths();
    }

    void queryReferrers(const nix::StorePath & path, nix::StorePathSet & referrers) override
    {
        if (auto r = ask(query_referrers_, paths, path)) {
            referrers.insert(r->begin(), r->end());
            return;
        }
        if (!underlying_)
            unsupported("queryReferrers");
        underlying_->queryReferrers(path, referrers);
    }

    void addTempRoot(const nix::StorePath & path) override
    {
        if (ask(add_temp_root_, [](nb::handle) { return true; }, path))
            return;
        if (!underlying_)
            unsupported("addTempRoot");
        underlying_->addTempRoot(path);
    }

    void addToStore(
        const nix::ValidPathInfo & info,
        nix::Source & narSource,
        nix::RepairFlag repair,
        nix::CheckSigsFlag checkSigs) override
    {
        below("addToStore").addToStore(info, narSource, repair, checkSigs);
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

    void registerDrvOutput(const nix::Realisation & output) override
    {
        below("registerDrvOutput").registerDrvOutput(output);
    }

    void narFromPath(const nix::StorePath & path, nix::Sink & sink) override
    {
        below("narFromPath").narFromPath(path, sink);
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
    nix::ref<const PyStoreConfig> config_;
    std::shared_ptr<nix::Store> underlying_;
    // A hook is looked up once, at open. Null when the class defines
    // none.
    std::shared_ptr<PyRef> is_valid_path_, query_path_info_, query_path_from_hash_part_, query_all_valid_paths_,
        query_referrers_, add_temp_root_;
    std::shared_ptr<PyRef> impl_;

    static std::shared_ptr<PyRef> hook(const nb::object & impl, const char * name)
    {
        if (!nb::hasattr(impl, name))
            return nullptr;
        return std::make_shared<PyRef>(nb::getattr(impl, name));
    }

    static nix::StorePathSet paths(nb::handle h)
    {
        nix::StorePathSet out;
        for (nb::handle item : h)
            out.insert(nb::cast<nix::StorePath>(item));
        return out;
    }

    /**
     * The hook's answer, read by `read` under the GIL. Nullopt when the
     * class has no such hook, or the hook answered `NotImplemented`.
     */
    template<typename Read, typename... Args>
    static auto ask(const std::shared_ptr<PyRef> & hook, Read read, const Args &... args)
        -> std::optional<decltype(read(nb::handle()))>
    {
        if (!hook)
            return std::nullopt;
        nb::gil_scoped_acquire gil;
        nb::object answer = hook->get()(args...);
        if (answer.is(nb::handle(Py_NotImplemented)))
            return std::nullopt;
        return read(answer);
    }

    nix::Store & below(const char * op)
    {
        if (!underlying_)
            unsupported(op);
        return *underlying_;
    }
};

inline nix::ref<nix::Store> PyStoreConfig::openStore() const
{
    nb::gil_scoped_acquire gil;
    nb::dict params;
    for (auto & [k, v] : own)
        params[nb::str(k.c_str())] = nb::str(v.c_str());
    nb::object impl = factory->get()(scheme, authority, params);
    std::shared_ptr<nix::Store> underlying;
    if (nb::object u = nb::getattr(impl, "underlying", nb::none()); !u.is_none())
        underlying = nb::cast<std::shared_ptr<nix::Store>>(u);
    return nix::make_ref<PyStore>(
        nix::ref<const PyStoreConfig>(shared_from_this()), std::move(impl), std::move(underlying));
}

/**
 * Make `schemes` open a store that `factory` implements, for the rest
 * of the process.
 *
 * Nix's registry is a static, destroyed after Python. So the entries
 * leave it at `atexit`, while the interpreter can still release each
 * factory and what it holds; nanobind reports every instance a factory
 * closes over as a leak otherwise.
 */
inline void register_python_store(const std::string & name, const std::vector<std::string> & schemes, nb::object factory)
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
        .doc = "A store implemented in Python.",
        .uriSchemes = {schemes.begin(), schemes.end()},
        .experimentalFeature = std::nullopt,
        .parseConfig = [held](std::string_view scheme, std::string_view authority, const nix::StoreConfig::Params & params)
            -> nix::ref<nix::StoreConfig> { return nix::make_ref<PyStoreConfig>(held, scheme, authority, params); },
        .getConfig = [held]() -> nix::ref<nix::StoreConfig> {
            return nix::make_ref<PyStoreConfig>(held, "", "", nix::StoreConfig::Params{});
        },
    };
    if (!nix::Implementations::registered().emplace(name, std::move(made)).second)
        throw nix::Error("a store named '%s' is already registered", name);
    ours.push_back(name);
}

} // namespace huggorm
