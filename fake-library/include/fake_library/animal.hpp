#pragma once
#include <string>

namespace fake_library {

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
