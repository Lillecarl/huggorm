#include "fake_library/store.hpp"

#include <algorithm>
#include <chrono>
#include <cctype>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <thread>

namespace fake_library {

namespace {

// Deliberate latency so the async layer has real work to overlap.
void pretend_io_work(int ms)
{
    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
}

constexpr char kBase32Alphabet[] = "0123456789abcdfghijklmnpqrsvwxyz";  // Nix alphabet (no e, o, u, t)

std::string fake_hash(const std::string & seed)
{
    // FNV-1a, expanded into HashLen base-32 chars. Not a real hash:
    // deterministic, collision-prone at toy scale, good enough to demo.
    uint64_t h = 1469598103934665603ull;
    for (unsigned char c : seed) {
        h ^= c;
        h *= 1099511628211ull;
    }
    std::string out;
    out.reserve(StorePath::HashLen);
    for (size_t i = 0; i < StorePath::HashLen; ++i) {
        out += kBase32Alphabet[(h >> (i % 13)) % 32];
        h = h * 31 + i;
    }
    return out;
}

bool valid_name_char(char c)
{
    return std::isalnum(static_cast<unsigned char>(c)) || c == '.' || c == '-' || c == '_' || c == '+';
}

}  // namespace

StorePath::StorePath(std::string hash, std::string name)
    : hash_(std::move(hash)), name_(std::move(name))
{
    if (hash_.size() != HashLen
        || !std::all_of(hash_.begin(), hash_.end(), [](char c) {
               return std::strchr(kBase32Alphabet, c) != nullptr;
           }))
        throw std::invalid_argument("store path hash must be " + std::to_string(HashLen) + " base-32 chars");
    if (name_.empty() || name_.front() == '.'
        || !std::all_of(name_.begin(), name_.end(), valid_name_char))
        throw std::invalid_argument("invalid store path name: " + name_);
}

StorePath::StorePath(std::string base_name)
{
    // Same shape as the real nix::StorePath(std::string_view): exactly
    // HashLen base-32 chars, then '-', then a name.
    if (base_name.size() <= HashLen + 1 || base_name[HashLen] != '-')
        throw std::invalid_argument("store path must be <hash>-<name>: " + base_name);
    *this = StorePath(base_name.substr(0, HashLen), base_name.substr(HashLen + 1));
}

std::string StorePath::to_string() const { return hash_ + "-" + name_; }
std::string StorePath::hash() const { return hash_; }
std::string StorePath::name() const { return name_; }

Derivation::Derivation(std::string name) : name_(std::move(name)) {}

void Derivation::set_env(std::string key, std::string value)
{
    env_[std::move(key)] = std::move(value);
}

std::string Derivation::describe()
{
    ++queries_;
    return name_ + " (" + std::to_string(env_.size()) + " env entries, seen " + std::to_string(queries_) + "x)";
}

int Derivation::queries() const { return queries_; }

DerivedPath::DerivedPath(StorePath path) : path_(std::move(path)), built_(false) {}

DerivedPath::DerivedPath(StorePath drv_path, std::string output)
    : path_(std::move(drv_path)), built_(true), output_(std::move(output))
{}

std::string DerivedPath::describe() const
{
    if (built_)
        return "built " + path_.to_string() + "^" + output_;
    return "opaque " + path_.to_string();
}

const StorePath & DerivedPath::path() const { return path_; }

const std::string & DerivedPath::output_name() const { return output_; }

bool DerivedPath::is_built() const { return built_; }

Store::Store() : lock_(new std::mutex()) {}

Store::~Store()
{
    delete static_cast<std::mutex *>(lock_);
}

void Store::register_(const std::string & base_name) const
{
    std::lock_guard<std::mutex> guard(*static_cast<std::mutex *>(lock_));
    valid_.insert(base_name);
}

bool Store::lookup(const std::string & base_name) const
{
    std::lock_guard<std::mutex> guard(*static_cast<std::mutex *>(lock_));
    return valid_.count(base_name) > 0;
}

bool Store::is_valid_path(const StorePath & path) const
{
    return lookup(path.to_string());
}

StorePath Store::add_text_to_store(std::string name, std::string contents) const
{
    pretend_io_work(100);
    StorePath path(fake_hash(contents), std::move(name));
    register_(path.to_string());
    return path;
}

StorePath Store::build_derivation(const DerivedPath & request) const
{
    if (request.is_built()) {
        // Built request: derive an output path from the drv path name.
        pretend_io_work(120);
        StorePath out(fake_hash(request.describe()), request.path().name() + "-" + request.output_name());
        register_(out.to_string());
        return out;
    }
    // Opaque request: nothing to build, but it must already exist.
    if (!lookup(request.path().to_string()))
        throw std::invalid_argument("path is not valid: " + request.path().to_string());
    return request.path();
}

Derivation Store::query_derivation(const StorePath & drv_path) const
{
    pretend_io_work(60);
    const std::string base = drv_path.to_string();
    if (!lookup(base))
        throw std::invalid_argument("path is not valid: " + base);
    if (drv_path.name().size() < 4 || drv_path.name().substr(drv_path.name().size() - 4) != ".drv")
        throw std::invalid_argument("not a derivation: " + base);
    Derivation drv(drv_path.name().substr(0, drv_path.name().size() - 4));
    drv.set_env("builder", "bash");
    drv.set_env("system", "x86_64-linux");
    return drv;
}

std::string LocalStore::get_uri() const { return "local"; }

std::string RemoteStore::get_uri() const { return "uds://daemon"; }

std::string describe_store(const Store & store)
{
    return "store(" + store.get_uri() + ")";
}

}  // namespace fake_library
