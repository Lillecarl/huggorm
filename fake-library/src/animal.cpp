#include "fake_library/animal.hpp"

namespace fake_library {

Poop::Poop(std::string producer) : producer_(std::move(producer)), inspections_(0) {}

std::string Poop::describe() {
    inspections_++;
    return producer_ + "'s poop (inspected " + std::to_string(inspections_) + "x)";
}

int Poop::inspections() const { return inspections_; }

Ball::Ball(std::string color) : color_(std::move(color)) {}

std::string Ball::describe() const { return color_ + " ball"; }

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

Poop Animal::poop() const { return Poop(name_); }

Ball Animal::toy() const { return Ball("red"); }

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
