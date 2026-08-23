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
    }
    return "unknown";
}

long long Value::integer() const
{
    if (!forced_)
        throw std::runtime_error("value is a thunk");
    if (kind_ != Kind::Int)
        throw std::runtime_error("value is not an integer");
    return int_;
}

std::string Value::string_value() const
{
    if (!forced_)
        throw std::runtime_error("value is a thunk");
    if (kind_ != Kind::String)
        throw std::runtime_error("value is not a string");
    return str_;
}

bool Value::boolean() const
{
    if (!forced_)
        throw std::runtime_error("value is a thunk");
    if (kind_ != Kind::Bool)
        throw std::runtime_error("value is not a boolean");
    return bool_;
}

EvalState::EvalState(std::string store_uri) : store_uri_(std::move(store_uri)) {}

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

Value EvalState::parse_expr(const std::string & expr) { return parse_(expr); }

Value EvalState::eval_expr(const std::string & expr)
{
    Value v = parse_(expr);
    pretend_eval_work(40);  // evaluation costs more than parsing
    v.force();
    return v;
}

void EvalState::force(Value & v) { v.force(); }

}  // namespace fake_library
