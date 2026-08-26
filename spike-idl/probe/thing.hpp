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
