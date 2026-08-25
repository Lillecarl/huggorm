#include "fake_library/eval.hpp"

#include <chrono>
#include <cctype>
#include <stdexcept>
#include <thread>

namespace fake_library {

namespace {

// Evaluation and forcing are deliberately slow: the async layer has to
// overlap work on other runners while this one serializes.
void pretend_eval_work(int ms)
{
    std::this_thread::sleep_for(std::chrono::milliseconds(ms));
}

std::string trim(const std::string & s)
{
    size_t b = 0, e = s.size();
    while (b < e && std::isspace(static_cast<unsigned char>(s[b]))) ++b;
    while (e > b && std::isspace(static_cast<unsigned char>(s[e - 1]))) --e;
    return s.substr(b, e - b);
}

}  // namespace

Value Value::make(long long v, bool forced)
{
    Value out;
    out.kind_ = Kind::Int;
    out.forced_ = forced;
    out.int_ = v;
    return out;
}

Value Value::make(std::string v, bool forced)
{
    Value out;
    out.kind_ = Kind::String;
    out.forced_ = forced;
    out.str_ = std::move(v);
    return out;
}

Value Value::make(bool v)
{
    Value out;
    out.kind_ = Kind::Bool;
    out.forced_ = true;
    out.bool_ = v;
    return out;
}

void Value::force()
{
    if (forced_)
        return;  // forceValue is idempotent
    pretend_eval_work(20);
    forced_ = true;
}

std::string Value::type_name() const
{
    if (!forced_)
        return "thunk";
    switch (kind_) {
        case Kind::Int: return "int";
        case Kind::String: return "string";
        case Kind::Bool: return "bool";
        case Kind::List: return "list";
        case Kind::Attrs: return "attrs";
    }
    return "unknown";
}

void Value::want(Kind k, const char * what) const
{
    if (!forced_)
        throw std::runtime_error("value is a thunk");
    if (kind_ != k)
        throw std::runtime_error(std::string("value is not ") + what);
}

long long Value::integer() const
{
    want(Kind::Int, "an integer");
    return int_;
}

std::string Value::string_value() const
{
    want(Kind::String, "a string");
    return str_;
}

bool Value::boolean() const
{
    want(Kind::Bool, "a boolean");
    return bool_;
}

size_t Value::size() const
{
    if (!forced_)
        throw std::runtime_error("value is a thunk");
    if (kind_ == Kind::List)
        return list_.size();
    if (kind_ == Kind::Attrs)
        return attrs_.size();
    throw std::runtime_error("value is not a list or an attribute set");
}

Value * Value::at(size_t index) const
{
    want(Kind::List, "a list");
    if (index >= list_.size())
        throw std::runtime_error("list index out of range");
    return list_[index];
}

std::vector<std::string> Value::names() const
{
    want(Kind::Attrs, "an attribute set");
    // std::map iterates in key order, so the caller gets a stable
    // listing without sorting it again.
    std::vector<std::string> out;
    out.reserve(attrs_.size());
    for (const auto & [name, _] : attrs_)
        out.push_back(name);
    return out;
}

bool Value::has(const std::string & name) const
{
    want(Kind::Attrs, "an attribute set");
    return attrs_.find(name) != attrs_.end();
}

Value * Value::get(const std::string & name) const
{
    want(Kind::Attrs, "an attribute set");
    const auto it = attrs_.find(name);
    if (it == attrs_.end())
        throw std::runtime_error("attribute '" + name + "' is missing");
    return it->second;
}

EvalState::EvalState(std::string store_uri) : store_uri_(std::move(store_uri))
{
    gcenv::init();
}

std::string EvalState::get_store_uri() const { return store_uri_; }

Value EvalState::parse_(const std::string & expr) const
{
    const std::string t = trim(expr);
    if (t.empty())
        throw std::invalid_argument("empty expression");
    if (t == "true") return Value::make(true);
    if (t == "false") return Value::make(false);
    if (t.size() >= 2 && t.front() == '"' && t.back() == '"')
        return Value::make(t.substr(1, t.size() - 2), false);
    bool digits = !t.empty();
    for (char c : t)
        if (!std::isdigit(static_cast<unsigned char>(c)))
            { digits = false; break; }
    if (digits)
        return Value::make(std::stoll(t), false);
    throw std::invalid_argument("parse error: " + t);
}

Value * EvalState::parse_expr(const std::string & expr) { return new Value(parse_(expr)); }

Value * EvalState::eval_expr(const std::string & expr)
{
    Value v = parse_(expr);
    pretend_eval_work(40);  // evaluation costs more than parsing
    v.force();
    return new Value(std::move(v));
}

void EvalState::force(Value * v) { v->force(); }

Value * EvalState::make_int(long long v) { return new Value(Value::make(v, true)); }

Value * EvalState::make_string(const std::string & v)
{
    return new Value(Value::make(v, true));
}

Value * EvalState::make_bool(bool v) { return new Value(Value::make(v)); }

Value * EvalState::make_list(const std::vector<Value *> & items)
{
    // Allocate the value FIRST, then fill it. Its List member allocates
    // from the GC heap, and the collector can only see those nodes
    // through a value it can already reach.
    Value * out = new Value();
    out->kind_ = Value::Kind::List;
    out->forced_ = true;
    for (Value * item : items) {
        if (item == nullptr)
            throw std::invalid_argument("null element in a list");
        out->list_.push_back(item);
    }
    return out;
}

Value * EvalState::make_attrs(const std::vector<std::pair<std::string, Value *>> & items)
{
    Value * out = new Value();
    out->kind_ = Value::Kind::Attrs;
    out->forced_ = true;
    for (const auto & [name, item] : items) {
        if (item == nullptr)
            throw std::invalid_argument("null value for attribute '" + name + "'");
        // Last one wins, matching an attribute set built by assignment.
        out->attrs_[name] = item;
    }
    return out;
}

}  // namespace fake_library
