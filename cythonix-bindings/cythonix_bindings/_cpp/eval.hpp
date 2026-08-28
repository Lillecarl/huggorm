#pragma once

/**
 * The C++ a declaration could not have written, for the evaluator.
 *
 * Two classes, and each exists for a reason libexpr forces on us.
 *
 * `Evaluator` owns what a `nix::EvalState` needs and does not own.
 * Upstream says both settings objects "must outlive the lifetime of
 * this EvalState", and the store is a `ref<Store>` the state keeps a
 * share of. One owner holds all four, so the order is stated once
 * here rather than depended on from three places.
 *
 * `Bridge` is a ROOT. A `nix::Value` lives in the collector's heap
 * and belongs to nobody: it stays alive for exactly as long as the
 * collector can SEE a pointer to it, and the collector scans
 * registered thread stacks and its own heap - not Python's. A Python
 * object holding a bare `Value *` roots nothing.
 *
 * The root is upstream's. `nix::allocRootValue` returns a
 * `std::shared_ptr<Value *>` allocated from a traceable allocator
 * (value.hh:1444, eval.cc:105), which is the same trick a hand-rolled
 * `GC_malloc_uncollectable` cell used to do here. Ours is gone: a
 * hand root beside an upstream root is one fact declared twice.
 *
 * EVERY Bridge takes its OWN root. A child never borrows its
 * parent's. The two lifetimes are different mechanisms - the server's
 * `parents=[self]` keeps the STATE leased, and the root keeps the
 * VALUE reachable - so a client that drops a parent handle and keeps
 * a child needs both, and only one of them is the lease.
 *
 * A Bridge also carries its Evaluator, which is not bookkeeping. A
 * `nix::Value` is not self-describing: an attribute name is a
 * `Symbol`, a `uint32_t` index into the producing state's
 * `SymbolTable`, so rendering one needs the state in hand. The mock's
 * value carried its own strings and hid this.
 */

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "nix/expr/attr-set.hh"
#include "nix/expr/eval-gc.hh"
#include "nix/expr/eval-settings.hh"
#include "nix/expr/eval.hh"
#include "nix/expr/nixexpr.hh"
#include "nix/expr/symbol-table.hh"
#include "nix/expr/value.hh"
#include "nix/fetchers/fetch-settings.hh"
#include "nix/store/store-api.hh"
#include "nix/store/store-open.hh"

#include <gc/gc.h>

namespace cythonix {

// ---- the collector, and the threads Python made -------------------

/**
 * Registers the calling thread with the collector, once.
 *
 * Executor threads are created by Python and are invisible to the
 * collector's pthread interception. Each must register before
 * touching GC memory: allocation from an unregistered thread races
 * with a collection.
 *
 * `nix::initGC()` already calls `GC_allow_register_threads()`
 * (eval-gc.cc:68), so the permission is upstream's and only the
 * per-thread half is ours.
 *
 * The flag records whether WE registered this thread.
 * `GC_register_my_thread` answers `GC_DUPLICATE` for a thread the
 * collector already knows - the main thread, or one it created - and
 * unregistering such a thread is not ours to do.
 */
inline bool & gc_owns_registration()
{
    static thread_local bool flag = false;
    return flag;
}

/**
 * Whether this thread has been ASKED yet, as opposed to whether we
 * own its registration.
 *
 * Two flags, not one, and the difference cost 2.25 ms per value.
 * `GC_register_my_thread` answers GC_DUPLICATE for a thread the
 * collector already knows - the main thread, every time - so a single
 * flag set only on GC_SUCCESS never latched there, and every
 * Bridge, every stage and every destructor called
 * `GC_get_stack_base` again. That call is not cheap: it reads the
 * process's own memory map to find the stack bounds.
 *
 * Measured, not reasoned: 500 `make_int` calls went from 1124 ms to
 * 0.17 ms. The allocator was never the problem - swapping
 * `nix::allocRootValue` for a hand-rolled uncollectable cell changed
 * the figure by 3 ms in 1124, which is what ruled it out.
 */
inline bool & gc_asked()
{
    static thread_local bool flag = false;
    return flag;
}

inline void gc_register_thread()
{
    if (gc_asked())
        return;
    gc_asked() = true;
    struct GC_stack_base sb;
    GC_get_stack_base(&sb);
    // GC_SUCCESS means WE registered it, so we are the ones who must
    // unregister. GC_DUPLICATE means the collector already knew, and
    // unregistering such a thread is not ours to do.
    if (GC_register_my_thread(&sb) == GC_SUCCESS)
        gc_owns_registration() = true;
}

/**
 * Takes the CURRENT thread off the collector's list, as its last GC
 * action before it exits.
 *
 * Boehm stops the world by signalling every registered thread and
 * waiting for each to answer. A thread that exits while still
 * registered never answers, and the next collection aborts the
 * PROCESS with "Signals delivery fails constantly". A dedicated
 * affine executor shuts its single thread down on close, which is
 * exactly that shape.
 */
inline void gc_unregister_thread()
{
    if (!gc_owns_registration())
        return;
    GC_unregister_my_thread();
    gc_owns_registration() = false;
    gc_asked() = false;
}

inline void gc_collect()
{
    // Two cycles: finalizers and frees lag one behind.
    gc_register_thread();
    GC_gcollect();
    GC_gcollect();
}

class Bridge;

/**
 * How many roots this process is holding right now.
 *
 * OUR bookkeeping, not the collector's, and that is the point. Heap
 * counters answer a question about boehm - which is conservative, so
 * a stale pointer in a register legitimately retains an object and a
 * byte-count assertion is flaky by design. This answers a question
 * about US: did every Bridge that was made release its root.
 *
 * A leak of roots is a real bug class and nothing else can see it: a
 * root that is never dropped keeps its value alive forever, and the
 * heap only says the heap grew.
 */
inline std::atomic<std::size_t> & live_roots()
{
    static std::atomic<std::size_t> count{0};
    return count;
}

// ---- one evaluator, and everything it outlives --------------------

/**
 * Everything a `nix::EvalState` needs and does not own, on the heap
 * and shared.
 *
 * The Python object and every Bridge hold a share, so the state dies
 * with whichever of them goes last - which is producer pinning as a
 * C++ fact rather than a rule somebody has to keep.
 *
 * It is a separate object rather than `enable_shared_from_this` on
 * the bound class. nanobind does not construct a bound instance
 * through `std::make_shared`, so the weak reference that
 * `shared_from_this` needs is never armed and the first call throws
 * `bad_weak_ptr` - verified, not guessed.
 *
 * Member order IS construction order, and it is not arbitrary:
 * `EvalSettings` takes `read_only_` BY REFERENCE and `EvalState`
 * takes both settings objects by reference, so each has to already
 * exist. One class is what makes that a compile-time fact rather
 * than a comment.
 */
class EvalCore
{
public:
    explicit EvalCore(const std::string & store_uri)
        : store_uri_(store_uri)
        , eval_settings_(read_only_)
        , store_(nix::openStore(store_uri))
        , state_(nix::LookupPath{}, store_, fetch_settings_, eval_settings_)
    {
    }

    EvalCore(const EvalCore &) = delete;
    EvalCore & operator=(const EvalCore &) = delete;

    nix::EvalState & state() { return state_; }

    const std::string & store_uri() const { return store_uri_; }

private:
    std::string store_uri_;
    bool read_only_ = false;
    nix::fetchers::Settings fetch_settings_;
    nix::EvalSettings eval_settings_;
    nix::ref<nix::Store> store_;
    nix::EvalState state_;
};

/**
 * A core whose DELETER registers the calling thread.
 *
 * `~EvalState` tears down GC-resident structures, and the last share
 * can be dropped by any holder on any thread: an event loop, a pool
 * worker, the server's reaper, a Python finalizer. Putting the
 * registration in the deleter covers every holder that exists and
 * every one that has not been written yet, once, instead of asking
 * each new class to remember.
 *
 * `~Bridge` still registers for itself. Its RootValue deallocates
 * during MEMBER destruction whether or not it holds the last share of
 * the core, so the deleter would not always run for it.
 */
inline std::shared_ptr<EvalCore> make_core(const std::string & store_uri)
{
    return {new EvalCore(store_uri), [](EvalCore * core) {
                gc_register_thread();
                delete core;
            }};
}

class Evaluator
{
public:
    explicit Evaluator(const std::string & store_uri)
        : core_(make_core(store_uri))
    {
        gc_register_thread();
    }

    nix::EvalState & state() const { return core_->state(); }

    const std::string & get_store_uri() const { return core_->store_uri(); }

    // -- production ------------------------------------------------
    //
    // Every one of these returns a Bridge, so the declaration binds
    // them by name and writes no body. A method that handed back a
    // bare `nix::Value *` would need each call site to root it, which
    // is the rule this file exists to keep in one place.

    Bridge parse_expr(const std::string & expr);
    Bridge eval_expr(const std::string & expr);

    Bridge make_int(std::int64_t value);
    Bridge make_string(const std::string & value);
    Bridge make_bool(bool value);
    Bridge make_list();
    Bridge make_attrs();

    void force(const Bridge & v);
    void list_append(const Bridge & target, const Bridge & item);
    void attrs_set(const Bridge & target, const std::string & name,
                   const Bridge & item);

private:
    Bridge wrap(nix::Value * v);

    std::shared_ptr<EvalCore> core_;
};

// ---- one rooted value ---------------------------------------------

class Bridge
{
public:
    Bridge(std::shared_ptr<EvalCore> core, nix::Value * value)
        : core_(std::move(core))
        , root_(nix::allocRootValue(value))
    {
        live_roots().fetch_add(1, std::memory_order_relaxed);
    }

    /**
     * Registers the thread, then lets the root go.
     *
     * This runs on WHICHEVER thread drops the last Python reference,
     * and that is rarely the thread that produced the value: an event
     * loop, a pool worker, the server's reaper. The RootValue is a
     * `shared_ptr` over traceable-allocator memory, so releasing the
     * last share deallocates from the GC heap. Reading needs no
     * registration - a root keeps a value reachable from anywhere -
     * but freeing does.
     *
     * The core needs no registration here: its deleter carries one,
     * so every holder is covered rather than each remembering.
     *
     * The body runs BEFORE the members are destroyed, which is what
     * makes registering here the right place rather than a race with
     * itself.
     */
    ~Bridge()
    {
        gc_register_thread();
        live_roots().fetch_sub(1, std::memory_order_relaxed);
    }

    // A copy is a second root over the same value, and it counts as
    // one: the count is of ROOTS, not of distinct values.
    Bridge(const Bridge & other)
        : core_(other.core_), root_(other.root_), by_name_(other.by_name_)
    {
        live_roots().fetch_add(1, std::memory_order_relaxed);
    }

    Bridge & operator=(const Bridge &) = default;

    nix::Value * get() const { return *root_; }

    nix::EvalState & state() const { return core_->state(); }

    const std::shared_ptr<EvalCore> & core() const { return core_; }

    /**
     * The underlying value's address, as a number.
     *
     * What makes two wrappers THE SAME node. A fresh wrapper is built
     * for every access, so Python identity says nothing: two wrappers
     * over one value differ, and a wrapper that dies hands its id()
     * to the next one. The VALUE pointer is the identity, and only
     * this file knows where it lives.
     */
    std::uintptr_t identity() const
    {
        return reinterpret_cast<std::uintptr_t>(*root_);
    }

    /** Whether this value sits inside a GC-allocated block. */
    bool is_gc_managed() const { return GC_base(*root_) != nullptr; }

    // -- reading ---------------------------------------------------
    //
    // Every accessor GUARDS. `nix::Value` is a tagged union whose
    // readers are `noexcept` and undefined on the wrong tag - reading
    // `integer()` off a string is not an error, it is a
    // reinterpretation of the payload. The mock checked for us, which
    // is the one place it was flattering: nothing here may go through
    // a bare pointer-to-member.

    std::string type_name() const;
    std::int64_t integer() const;
    std::string string_value() const;
    bool boolean() const;
    std::int64_t size() const;
    Bridge at(std::int64_t index) const;
    std::string name_at(std::int64_t index) const;
    Bridge value_at(std::int64_t index) const;
    bool has(const std::string & name) const;
    Bridge get_attr(const std::string & name) const;

private:
    const nix::Attr & attr_at(std::int64_t index) const;

    std::shared_ptr<EvalCore> core_;
    nix::RootValue root_;
    // Attributes in NAME order, built on first indexed access.
    //
    // nix::Bindings is sorted by Symbol ID, which is INTERNING order -
    // the order the names were first seen anywhere in the process, not
    // alphabetical. `lexicographicOrder` is the accessor that hides
    // that, and it costs a sort.
    //
    // Cached because the tree walk reads name_at(i) and value_at(i)
    // for every i against ONE Bridge, so sorting per access would make
    // a walk quadratic in the number of attributes.
    mutable std::vector<const nix::Attr *> by_name_;
};

// ---- Evaluator, out of line ---------------------------------------

inline Bridge Evaluator::wrap(nix::Value * v)
{
    return Bridge(core_, v);
}

inline Bridge Evaluator::parse_expr(const std::string & expr)
{
    gc_register_thread();
    if (expr.empty())
        throw std::invalid_argument("empty expression");
    // A THUNK, which is what "parse without evaluating" means here.
    // `parseExprFromString` gives an Expr, not a Value, so the value
    // is one that evaluates that expression when it is forced.
    auto * e = state().parseExprFromString(expr, state().rootPath("."));
    auto * v = state().allocValue();
    v->mkThunk(&state().baseEnv, e);
    return wrap(v);
}

inline Bridge Evaluator::eval_expr(const std::string & expr)
{
    gc_register_thread();
    if (expr.empty())
        throw std::invalid_argument("empty expression");
    auto * e = state().parseExprFromString(expr, state().rootPath("."));
    auto * v = state().allocValue();
    state().eval(e, *v);
    state().forceValue(*v, nix::noPos);
    return wrap(v);
}

inline Bridge Evaluator::make_int(std::int64_t value)
{
    gc_register_thread();
    auto * v = state().allocValue();
    v->mkInt(value);
    return wrap(v);
}

inline Bridge Evaluator::make_string(const std::string & value)
{
    gc_register_thread();
    auto * v = state().allocValue();
    v->mkString(value, state().mem);
    return wrap(v);
}

inline Bridge Evaluator::make_bool(bool value)
{
    gc_register_thread();
    auto * v = state().allocValue();
    v->mkBool(value);
    return wrap(v);
}

inline Bridge Evaluator::make_list()
{
    gc_register_thread();
    auto * v = state().allocValue();
    v->mkList(state().buildList(0));
    return wrap(v);
}

inline Bridge Evaluator::make_attrs()
{
    gc_register_thread();
    auto * v = state().allocValue();
    auto builder = state().buildBindings(0);
    v->mkAttrs(builder);
    return wrap(v);
}

inline void Evaluator::force(const Bridge & v)
{
    gc_register_thread();
    state().forceValue(*v.get(), nix::noPos);
}

/**
 * A Nix list is IMMUTABLE and sized when it is built, so appending
 * builds a new one and repoints the value.
 *
 * That makes filling a list quadratic, and the builder exists anyway
 * because the wire cannot carry a container of proxies: one lease per
 * element is not something anything grants in bulk. A
 * `make_list(items)` taking a `list[Value]` would have no RPC surface
 * at all, so the shape that crosses is one element per call.
 */
inline void Evaluator::list_append(const Bridge & target, const Bridge & item)
{
    gc_register_thread();
    auto * v = target.get();
    if (v->type() != nix::nList)
        throw std::invalid_argument("value is not a list");
    auto old = v->listView();
    auto builder = state().buildList(old.size() + 1);
    std::size_t i = 0;
    for (auto * elem : old)
        builder[i++] = elem;
    builder[i] = item.get();
    v->mkList(builder);
}

/** As `list_append`: an attribute set is immutable, so this rebuilds. */
inline void Evaluator::attrs_set(const Bridge & target,
                                 const std::string & name,
                                 const Bridge & item)
{
    gc_register_thread();
    auto * v = target.get();
    if (v->type() != nix::nAttrs)
        throw std::invalid_argument("value is not an attribute set");
    auto sym = state().symbols.create(name);
    const auto * old = v->attrs();
    auto builder = state().buildBindings(old->size() + 1);
    for (const auto & attr : *old)
        if (attr.name != sym)
            builder.insert(attr);
    builder.insert(sym, item.get());
    v->mkAttrs(builder);
}

// ---- Bridge, out of line ------------------------------------------

inline std::string Bridge::type_name() const
{
    // The names the declaration's `@tree` map keys on. A kind named
    // nowhere in that map crosses as a proxy, which is what makes
    // "thunk", "function", "external" and "failed" honest answers
    // rather than gaps.
    switch (get()->type<true>()) {
    case nix::nThunk: return "thunk";
    case nix::nInt: return "int";
    case nix::nFloat: return "float";
    case nix::nBool: return "bool";
    case nix::nString: return "string";
    case nix::nPath: return "path";
    case nix::nNull: return "null";
    case nix::nAttrs: return "attrs";
    case nix::nList: return "list";
    case nix::nFunction: return "function";
    case nix::nExternal: return "external";
    case nix::nFailed: return "failed";
    }
    return "unknown";
}

inline std::int64_t Bridge::integer() const
{
    auto * v = get();
    if (v->type<true>() == nix::nThunk)
        throw std::runtime_error("value is a thunk");
    if (v->type() != nix::nInt)
        throw std::runtime_error("value is not int");
    return v->integer().value;
}

inline std::string Bridge::string_value() const
{
    auto * v = get();
    if (v->type<true>() == nix::nThunk)
        throw std::runtime_error("value is a thunk");
    if (v->type() != nix::nString)
        throw std::runtime_error("value is not string");
    return std::string(v->string_view());
}

inline bool Bridge::boolean() const
{
    auto * v = get();
    if (v->type<true>() == nix::nThunk)
        throw std::runtime_error("value is a thunk");
    if (v->type() != nix::nBool)
        throw std::runtime_error("value is not bool");
    return v->boolean();
}

inline std::int64_t Bridge::size() const
{
    auto * v = get();
    if (v->type<true>() == nix::nThunk)
        throw std::runtime_error("value is a thunk");
    if (v->type() == nix::nList)
        return static_cast<std::int64_t>(v->listSize());
    if (v->type() == nix::nAttrs)
        return static_cast<std::int64_t>(v->attrs()->size());
    throw std::runtime_error("value is not a list or an attribute set");
}

inline Bridge Bridge::at(std::int64_t index) const
{
    auto * v = get();
    if (v->type<true>() != nix::nList)
        throw std::runtime_error("value is not a list");
    auto items = v->listView();
    if (index < 0 || static_cast<std::size_t>(index) >= items.size())
        throw std::runtime_error("list index out of range");
    return Bridge(core_, items[static_cast<std::size_t>(index)]);
}

/**
 * One attribute, by position in NAME order.
 *
 * `Bindings` keeps its attributes sorted, which is why an index means
 * the same thing on both sides of the wire and why the tree walk can
 * read a name and a value by the same number.
 */
inline const nix::Attr & Bridge::attr_at(std::int64_t index) const
{
    auto * v = get();
    if (v->type<true>() != nix::nAttrs)
        throw std::runtime_error("value is not an attribute set");
    const auto * attrs = v->attrs();
    if (by_name_.size() != attrs->size())
        by_name_ = attrs->lexicographicOrder(state().symbols);
    if (index < 0 || static_cast<std::size_t>(index) >= by_name_.size())
        throw std::runtime_error("attribute index out of range");
    return *by_name_[static_cast<std::size_t>(index)];
}

inline std::string Bridge::name_at(std::int64_t index) const
{
    // The Symbol is a uint32 index into the PRODUCING state's table,
    // which is the whole reason a Bridge carries its Evaluator.
    return std::string(state().symbols[attr_at(index).name]);
}

inline Bridge Bridge::value_at(std::int64_t index) const
{
    return Bridge(core_, attr_at(index).value);
}

inline bool Bridge::has(const std::string & name) const
{
    auto * v = get();
    if (v->type<true>() != nix::nAttrs)
        throw std::runtime_error("value is not an attribute set");
    return v->attrs()->get(state().symbols.create(name)) != nullptr;
}

inline Bridge Bridge::get_attr(const std::string & name) const
{
    auto * v = get();
    if (v->type<true>() != nix::nAttrs)
        throw std::runtime_error("value is not an attribute set");
    const auto * attr = v->attrs()->get(state().symbols.create(name));
    if (attr == nullptr)
        throw std::runtime_error("attribute '" + name + "' is missing");
    return Bridge(core_, attr->value);
}

}  // namespace cythonix
