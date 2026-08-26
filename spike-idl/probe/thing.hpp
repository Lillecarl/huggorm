#pragma once
#include <string>
#include <string_view>
#include <stdexcept>
namespace probe {
struct Thing {
    Thing() = delete;
    Thing(const Thing &) = default;
    explicit Thing(std::string n) : name_(std::move(n)) {
        if (name_.empty()) throw std::runtime_error("empty");
    }
    std::string_view name() const { return name_; }
    bool operator==(const Thing & o) const = default;
private:
    std::string name_;
};
}

namespace probe {
// A factory the pxd can give the RIGHT exception specification.
// make_shared carries libcpp's own `except +`, so a custom translator
// declared on the constructor never runs - the call Cython emits is
// std::make_shared, not the constructor.
inline std::shared_ptr<Thing> make_thing(std::string n)
{
    return std::make_shared<Thing>(std::move(n));
}
}
