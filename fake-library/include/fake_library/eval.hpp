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
// - A value can be a LIST or an ATTRIBUTE SET holding other values, so
//   a value is a tree and any node of it may still be a thunk.
//
// The expression language is a toy: integer literals, quoted strings,
// true/false. Enough to exercise parsing errors, forcing and typed
// access - not a real evaluator. Collections are BUILT, not parsed:
// reimplementing Nix's syntax would buy nothing the wire and lifetime
// paths do not already get from a builder.

#include <map>
#include <string>
#include <utility>
#include <vector>

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

    // "thunk", "int", "string", "bool", "list" or "attrs".
    std::string type_name() const;

    // Every accessor throws std::runtime_error when the value is still
    // a thunk, or when it does not match the value's kind.
    long long integer() const;
    std::string string_value() const;
    bool boolean() const;

    // Collections. The elements are values in their own right and each
    // may be an unforced thunk: forcing a list forces the list, not
    // what is in it, exactly as in libexpr.
    size_t size() const;                            // list or attrs
    Value * at(size_t index) const;                 // list
    std::vector<std::string> names() const;         // attrs, sorted
    bool has(const std::string & name) const;       // attrs
    Value * get(const std::string & name) const;    // attrs

private:
    friend class EvalState;
    enum class Kind { Int, String, Bool, List, Attrs };

    // The collector must SEE these pointers, so their nodes are
    // allocated from the GC heap. A plain std::vector<Value *> holds
    // its elements in malloc memory, which Boehm does not scan: the
    // children would be collected while this value still pointed at
    // them, and the next access would read freed memory.
    //
    // gc_allocator, not traceable_allocator: a Value derives from `gc`
    // and never runs its destructor, so a container that had to be
    // freed by hand would never be. GC memory needs no freeing.
    using List = std::vector<Value *, gc_allocator<Value *>>;
    using Attrs = std::map<std::string, Value *, std::less<>,
                           gc_allocator<std::pair<const std::string, Value *>>>;

    Kind kind_ = Kind::Int;
    bool forced_ = false;
    long long int_ = 0;
    std::string str_;
    bool bool_ = false;
    List list_;
    Attrs attrs_;

    static Value make(long long v, bool forced);
    static Value make(std::string v, bool forced);
    static Value make(bool v);
    void force();
    void want(Kind k, const char * what) const;
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

    // Builders. Nix evaluation hands back attribute sets far more often
    // than it hands back scalars, and the wire has to carry one; these
    // produce the shapes without a parser for them. All return forced
    // values: a thunk is what parse_expr is for.
    Value * make_int(long long v);
    Value * make_string(const std::string & v);
    Value * make_bool(bool v);
    Value * make_list(const std::vector<Value *> & items);
    Value * make_attrs(const std::vector<std::pair<std::string, Value *>> & items);

private:
    Value parse_(const std::string & expr) const;
    std::string store_uri_;
};

}  // namespace fake_library
