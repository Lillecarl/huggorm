#pragma once
#include <stdexcept>
#include <string>

namespace fake_library {
// Stateful value produced by animals. NOT thread-safe: carries mutable
// state, so all access must happen on the producer's thread.
class Poop {
public:
    explicit Poop(std::string producer);
    // Mutates: counts inspections.
    std::string describe();
    int inspections() const;

private:
    std::string producer_;
    int inspections_;
};

// Immutable value. THREAD-SAFE: const-only access, no mutable state.
class Ball {
public:
    explicit Ball(std::string color);
    std::string describe() const;

private:
    std::string color_;
};

// Base class with pure virtual methods.
// Cython will wrap this and allow Python to subclass it.
class Animal {
public:
    explicit Animal(std::string name);
    virtual ~Animal();

    // Pure virtuals: must be overridden
    virtual std::string speak() const = 0;
    virtual int legs() const = 0;

    // Non-virtual with std::string — tests string handling
    std::string get_name() const;
    void set_name(const std::string &name);

    // Throws std::invalid_argument for unknown items — tests C++ -> Python exception propagation
    std::string fetch(const std::string &item) const;

    // Produces a thread-unsafe value owned by this animal's context.
    Poop poop() const;

    // Produces a thread-safe value.
    Ball toy() const;

    // Deliberately slow operation: sleeps without touching shared state.
    // Safe to call with the GIL released.
    void wait_ms(int ms) const;

private:
    std::string name_;
};

class Cat : public Animal {
public:
    explicit Cat(std::string name);
    std::string speak() const override;
    int legs() const override;
};

class Dog : public Animal {
public:
    explicit Dog(std::string name);
    std::string speak() const override;
    int legs() const override;
};

// Free function that uses polymorphism — lets us test if Python overrides are visible to C++
std::string describe_animal(const Animal &animal);

} // namespace fake_library
