#include "fake_library/animal.hpp"

namespace fake_library {

Animal::Animal(std::string name) : name_(std::move(name)) {}
Animal::~Animal() = default;

std::string Animal::get_name() const { return name_; }
void Animal::set_name(const std::string &name) { name_ = name; }

std::string Animal::fetch(const std::string &item) const {
    if (item == "ball") {
        return name_ + " fetched the ball";
    }
    throw std::invalid_argument("unknown item: " + item);
}

Cat::Cat(std::string name) : Animal(std::move(name)) {}
std::string Cat::speak() const { return "meow"; }
int Cat::legs() const { return 4; }

Dog::Dog(std::string name) : Animal(std::move(name)) {}
std::string Dog::speak() const { return "woof"; }
int Dog::legs() const { return 4; }

std::string describe_animal(const Animal &animal) {
    return animal.get_name() + " says " + animal.speak() + " with " + std::to_string(animal.legs()) + " legs";
}

} // namespace fake_library
