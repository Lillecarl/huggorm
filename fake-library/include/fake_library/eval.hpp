#pragma once
// A small mock of the evaluation side of Nix. Shapes follow libexpr:
//
// - EvalState is NOT thread-safe. The real docs say to use one state per
//   thread; here that judgment becomes the affine policy.
// - Values live in the GC heap and belong to no one: like the real
//   libexpr, a value dies when the collector can no longer see any
//   reference to it, even while its producing EvalState lives on.
//   Bindings keep values alive by holding the pointer where the
//   collector can see it (see eval.pyx anchor blocks).
// - Values can be thunks: reading an unforced value throws, forcing it
//   mutates the value in place (the real forceValue does the same).
//
// The expression language is a toy: integer literals, quoted strings,
// true/false. Enough to exercise parsing errors, forcing and typed
// access - not a real evaluator.

#include <string>

#include "fake_library/gc-env.hpp"

namespace fake_library {

// Deriving from gc_base routes allocation into scanned GC memory. This
// is what makes non-reachability collection possible: nothing else has
// to remember a value for it to stay alive.
class Value : public gcenv::gc_base {
public:
    // Default state is invalid; exists only as binding glue.
    Value() = default;
    Value(const Value &) = default;

    std::string type_name() const;  // "thunk", "int", "string" or "bool"

    // All three throw std::runtime_error when the value is still a thunk,
    // or when the accessor does not match the value's kind.
    long long integer() const;
    std::string string_value() const;
    bool boolean() const;

private:
    friend class EvalState;
    enum class Kind { Int, String, Bool };
    Kind kind_ = Kind::Int;
    bool forced_ = false;
    long long int_ = 0;
    std::string str_;
    bool bool_ = false;

    static Value make(long long v, bool forced);
    static Value make(std::string v, bool forced);
    static Value make(bool v);
    void force();
};

// Plain C++ ownership again: the state allocates values but keeps none
// of them, so nothing in it needs collector visibility.
class EvalState {
public:
    explicit EvalState(std::string store_uri);

    std::string get_store_uri() const;

    // Parse without evaluating: the result is an unforced thunk.
    // Throws std::invalid_argument on a parse error.
    // The returned value is GC-owned; callers anchor it or lose it.
    Value * parse_expr(const std::string & expr);

    // Parse and evaluate: slow, and the result is fully forced.
    Value * eval_expr(const std::string & expr);

    // Force a value in place. Idempotent on already-forced values.
    void force(Value * v);

private:
    Value parse_(const std::string & expr) const;
    std::string store_uri_;
};

}  // namespace fake_library
