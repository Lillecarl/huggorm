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
#include <map>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

// `eval.hh` only forward-declares the concurrent map its caches are.
#include <boost/unordered/concurrent_flat_map.hpp>

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

namespace huggorm {

// ---- the file cache, which libexpr keeps to itself ----------------
//
// `tasks/016` wants the server to watch every file an evaluation
// read, so a change can invalidate the warm state rather than throw
// it away. libexpr KNOWS - `EvalState::fileEvalCache` is keyed by
// resolved path - and offers no way to ask: the member is private
// (`eval.hh:481`), and the accessor every read goes through is a
// `const ref<SourceAccessor>` the constructor builds from the
// settings alone (`eval.cc:267`), so there is nothing to substitute
// on the way in either. Upstream is moving further this way: 2.36pre
// makes `rootFS` private too.
//
// So the cache is reached the one way the standard allows without a
// patch. [temp.spec]/6: access checking is NOT performed on the names
// used in an explicit instantiation, so a template taking the member
// pointer as a non-type parameter may be instantiated with a private
// one, and the friend it defines hands it out afterwards. Legal,
// portable, and the reason Carl chose it over patching nixpkgs.
//
// A HELPER, not a mapping. It is the same shape as the GC root above:
// infrastructure over a foreign library that exposes no API for the
// fact, which generated code then CALLS. `cached_files` is what the
// declaration's `Cxx` body says, and the emitter writes the binding.
//
// It fails LOUDLY if upstream renames or removes the member: the
// explicit instantiation stops compiling. That is the right failure
// for a reach into a private, and better than a silent empty answer.

template <typename Tag, auto Member>
struct Reach
{
    friend auto get(Tag) { return Member; }
};

struct FileEvalCache
{
};
auto get(FileEvalCache);

template struct Reach<FileEvalCache, &nix::EvalState::fileEvalCache>;

struct ImportResolutionCache
{
};
auto get(ImportResolutionCache);

template struct Reach<ImportResolutionCache, &nix::EvalState::importResolutionCache>;

/**
 * Every file whose evaluation this state has cached, resolved.
 *
 * The set a filesystem change would invalidate. `import` goes through
 * `evalFile` (primops.cc:312), so a file reached from inside an
 * expression is here as surely as the one the caller named - which is
 * the whole reason this reads libexpr's cache rather than counting
 * what our own binding was asked to evaluate.
 *
 * Resolved, so `/foo` is here as `/foo/default.nix`: that is what the
 * cache is keyed by and what a watch has to name.
 */
inline std::vector<std::string> cached_files(const nix::EvalState & state)
{
    std::vector<std::string> out;
    (state.*get(FileEvalCache{}))->cvisit_all([&](const auto & entry) {
        out.push_back(entry.first.to_string());
    });
    return out;
}

/**
 * Forgets one file, so the next evaluation of it reads the disk.
 *
 * `resetFileCache()` is the only public way to do this, and it is not
 * a coarser version of the same thing: it also clears `inputCache`,
 * so one edited local file costs a re-fetch of every flake input over
 * the network. A state meant to live for days cannot pay that.
 *
 * TWO keys, because the caches are keyed differently. `fileEvalCache`
 * is keyed by the RESOLVED path, so forgetting `/foo` has to erase
 * `/foo/default.nix` - erasing only what the caller said would leave
 * the value cached, and the next evaluation would re-resolve, hit it,
 * and answer STALE. The resolution itself goes too: a symlink that
 * retargets, or a `/foo` that gains or loses a `default.nix`, changes
 * what `/foo` resolves TO, and re-resolving costs one stat.
 *
 * Collect first, erase after. `cvisit_all` holds a lock over the map
 * for the length of the visit, so an erase from inside the visitor is
 * a deadlock on that same map.
 *
 * Safe because an `EvalState` is AFFINE in this repo - one thread per
 * state at a time. That is huggorm's policy, not libexpr's guarantee:
 * the map itself tolerates concurrent writers, but nothing here
 * defends the read-then-erase against a racing evaluation refilling
 * the entry in between.
 *
 * A path that was never a key erases nothing, which is what makes a
 * caller free to feed a whole closure in without filtering it.
 */
inline void forget_file(nix::EvalState & state, const nix::SourcePath & given)
{
    std::vector<nix::SourcePath> evaluated{given};
    std::vector<nix::SourcePath> resolutions;

    (state.*get(ImportResolutionCache{}))->cvisit_all([&](const auto & entry) {
        if (entry.first == given)
            evaluated.push_back(entry.second);
        if (entry.first == given || entry.second == given)
            resolutions.push_back(entry.first);
    });

    for (const auto & key : evaluated)
        (state.*get(FileEvalCache{}))->erase(key);
    for (const auto & key : resolutions)
        (state.*get(ImportResolutionCache{}))->erase(key);
}

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

/**
 * Unregisters a thread we registered, when that thread EXITS.
 *
 * Boehm stops the world by signalling every registered thread and
 * waiting for each to answer. A thread that exits while still
 * registered never answers, and the next collection aborts the
 * PROCESS with "Signals delivery fails constantly" - no exception, no
 * traceback.
 *
 * A `thread_local` object's destructor runs at thread exit, so this
 * is the one place the rule cannot be forgotten. `gc_release_thread`
 * stays bound for a caller who wants to release early; nobody has to
 * remember it any more.
 *
 * Verified by reproducing the abort first: one pool thread made a
 * value, the pool shut down, and the next `collect_garbage()` killed
 * the process.
 */
struct ThreadExit
{
    ~ThreadExit()
    {
        if (gc_owns_registration()) {
            GC_unregister_my_thread();
            gc_owns_registration() = false;
        }
    }
};

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
    if (GC_register_my_thread(&sb) == GC_SUCCESS) {
        gc_owns_registration() = true;
        // Constructed on first registration, destroyed at thread exit.
        static thread_local ThreadExit at_exit;
        (void) at_exit;
    }
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

    // -- the two primitives every producer stands on ---------------
    //
    // `alloc` hands back uninitialised GC memory; `wrap` roots it and
    // ties it to this state. Between them goes ONE initialiser, which
    // is the only part that differs per producer and the only part a
    // declaration names.
    //
    // Neither mentions a Python method, and neither dies if a declared
    // method does - which is what makes them helpers rather than
    // bindings in a costume.

    nix::Value * alloc() const
    {
        gc_register_thread();
        return state().allocValue();
    }

    Bridge wrap(nix::Value * v) const;
    Bridge wrap_builder(nix::Value * v) const;


private:

    std::shared_ptr<EvalCore> core_;
};

// ---- one rooted value ---------------------------------------------

class Bridge
{
public:
    /**
     * `built` marks a value THIS binding made empty, for a caller to
     * fill. Only such a value accepts `stage`.
     *
     * A Nix value is immutable, and `mkList` on an evaluated one
     * rewrites memory the state may hold in a cache and other
     * Bridges may point at - verified: `eval_expr("[1 2 3]")` then an
     * append answered 4. The builders are the one place where
     * rewriting is safe, because nothing else has seen the value yet.
     */
    Bridge(std::shared_ptr<EvalCore> core, nix::Value * value,
           bool built = false)
        : built_(built)
        , core_(std::move(core))
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
        : built_(other.built_), core_(other.core_), root_(other.root_),
          by_name_(other.by_name_), staged_(other.staged_),
          staged_attrs_(other.staged_attrs_)
    {
        live_roots().fetch_add(1, std::memory_order_relaxed);
    }

    Bridge & operator=(const Bridge &) = default;

    /**
     * The rooted value, with any staged elements folded in first.
     *
     * Every read goes through here - our own accessors, and the
     * Evaluator when it takes a value as an ARGUMENT - so a staged
     * list can never be observed half-built. Making the fold happen
     * here rather than in each accessor is what makes that true by
     * construction instead of by remembering.
     */
    nix::Value * get() const
    {
        if (!staged_.empty() || !staged_attrs_.empty())
            materialise();
        return *root_;
    }

    nix::EvalState & state() const { return core_->state(); }

    /**
     * Adds one element to a list that is still being built.
     *
     * A Nix list is IMMUTABLE and sized when it is built, so the
     * obvious `list_append` rebuilds the whole list per call and
     * filling one is quadratic. One element per call is what the WIRE
     * requires - a container of proxies cannot cross - but it does not
     * require one rebuild per call. This stages, and `get()` builds
     * once.
     *
     * Each element is ROOTED as it arrives. A plain vector of
     * `nix::Value *` is memory the collector does not scan, so a
     * staged element with no other reference would be reclaimed
     * between two appends.
     */
    void stage(nix::Value * item) const
    {
        gc_register_thread();
        if (staged_.empty())
            // Seed from what the list already holds. Empty for a
            // fresh `make_list()`, and not for a list that came from
            // anywhere else. Only on the first append of a run: after
            // it, `staged_` is non-empty and IS the list.
            for (auto * elem : (*root_)->listView())
                staged_.push_back(nix::allocRootValue(elem));
        staged_.push_back(nix::allocRootValue(item));
    }

    /**
     * Whether this is a list, WITHOUT folding staged elements in.
     *
     * `get()` would materialise, and a caller that materialises before
     * every append pays the rebuild it was avoiding - which is the
     * quadratic coming back through the type check. Anything staged is
     * a list by construction, because only `stage` puts it there.
     */
    bool is_list() const
    {
        return !staged_.empty() || (*root_)->type<true>() == nix::nList;
    }

    /** Whether a caller may still fill this value. See the ctor. */
    bool is_builder() const { return built_; }

    /** As `is_list`, for an attribute set. */
    bool is_attrs() const
    {
        return !staged_attrs_.empty()
               || (*root_)->type<true>() == nix::nAttrs;
    }

    /**
     * Sets one attribute on a set that is still being built.
     *
     * An attribute set is immutable too, so the same rebuild-per-call
     * applies and the same staging answers it. Keyed by NAME rather
     * than by Symbol: interning is the state's business and there is
     * no reason to do it before the set is built.
     *
     * Setting a name twice replaces its value, which a map gives for
     * free and matches an attribute set built by assignment.
     */
    void stage_attr(const std::string & name, nix::Value * item) const
    {
        gc_register_thread();
        if (staged_attrs_.empty())
            for (const auto & attr : *(*root_)->attrs())
                staged_attrs_.insert_or_assign(
                    std::string(state().symbols[attr.name]),
                    nix::allocRootValue(attr.value));
        staged_attrs_.insert_or_assign(name, nix::allocRootValue(item));
    }

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

    /**
     * This value's attributes in NAME order.
     *
     * The FACT no declaration can state: `nix::Bindings` is sorted by
     * Symbol ID, which is INTERNING order - the order a name was
     * first seen anywhere in the process - not alphabetical.
     * `lexicographicOrder` is the accessor that hides it, and it
     * costs a sort.
     *
     * Cached, because a walk reads every index against ONE Bridge and
     * sorting per access would make it quadratic.
     */
    const std::vector<const nix::Attr *> & sorted() const
    {
        const auto * attrs = get()->attrs();
        if (by_name_.size() != attrs->size())
            by_name_ = attrs->lexicographicOrder(state().symbols);
        return by_name_;
    }

    /**
     * One Symbol, rendered.
     *
     * The other fact: a Symbol is a `uint32_t` index into the
     * PRODUCING state's table, so a name cannot be read off the value
     * alone.
     */
    std::string symbol(nix::Symbol s) const
    {
        return std::string(state().symbols[s]);
    }

    /** Interns a name, for a lookup. The state owns the table. */
    nix::Symbol intern(const std::string & name) const
    {
        return state().symbols.create(name);
    }

    /** A sibling value, sharing this one's root and state. */
    Bridge wrap(nix::Value * v) const { return Bridge(core_, v); }

private:

    // Whether this binding made the value for a caller to fill.
    bool built_ = false;

    /**
     * Folds staged elements into the value.
     *
     * The two stages are mutually exclusive by construction: a value
     * is a list or an attribute set, and only `stage` and
     * `stage_attr` fill them.
     */
    void materialise() const
    {
        gc_register_thread();
        if (!staged_.empty()) {
            auto builder = state().buildList(staged_.size());
            for (std::size_t i = 0; i < staged_.size(); ++i)
                builder[i] = *staged_[i];
            (*root_)->mkList(builder);
            staged_.clear();
            return;
        }
        auto builder = state().buildBindings(staged_attrs_.size());
        for (const auto & [name, value] : staged_attrs_)
            builder.insert(state().symbols.create(name), *value);
        (*root_)->mkAttrs(builder);
        staged_attrs_.clear();
    }

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
    // Elements of a list still being built, each rooted. See `stage`.
    mutable std::vector<nix::RootValue> staged_;
    // Attributes of a set still being built. See `stage_attr`.
    mutable std::map<std::string, nix::RootValue> staged_attrs_;
};

// ---- Evaluator, out of line ---------------------------------------

inline Bridge Evaluator::wrap(nix::Value * v) const
{
    return Bridge(core_, v);
}

/** As `wrap`, for a value a caller may still fill. */
inline Bridge Evaluator::wrap_builder(nix::Value * v) const
{
    return Bridge(core_, v, true);
}

// ---- Bridge, out of line ------------------------------------------

}  // namespace huggorm
