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

#include <cstddef>
#include <cstdint>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>

// `eval.hh` only forward-declares the concurrent map its caches are.
#include <boost/unordered/concurrent_flat_map.hpp>

// A LINE CROSSED, and worth naming. Everything else in this header is
// C++ over nix, and nanobind appears only in the emitted modules that
// include it. `register_primop` cannot be: a primop implemented in
// Python is a C++ callback that re-enters the interpreter, so the
// callable and the GIL are part of the fact this header carries.
//
// It is the ONLY thing here that needs it, and it stays that way -
// anything else reaching for nanobind is a mapping in a costume.
#include <nanobind/nanobind.h>

namespace nb = nanobind;

#include "nix/expr/attr-set.hh"
#include "nix/expr/eval-settings.hh"
#include "nix/expr/eval.hh"
#include "nix/expr/nixexpr.hh"
#include "nix/expr/symbol-table.hh"
#include "nix/expr/value.hh"
#include "nix/fetchers/fetch-settings.hh"
#include "nix/store/store-api.hh"
#include "nix/store/store-open.hh"

#include <gc/gc.h>

// The log tap and the collector, each in a file of its own. Carl:
// "there's no point in limiting the amount of files, separate as
// appropriate."
//
// Neither depends on the other and neither depends on the evaluator,
// which is what made them the two to move first. This file depends on
// the collector - `alloc` and every Bridge register a thread - so
// `gc.hpp` is included rather than merely adjacent.
//
// ABOVE the namespace, and that is not style. Included inside it they
// nest, and every name in them becomes `huggorm::huggorm::` - which
// is what the compiler said when they were first put where the code
// used to be.
#include "huggorm_decl/cpp/gc.hpp"
#include "huggorm_decl/cpp/logging.hpp"
#include "huggorm_decl/cpp/settings.hpp"

namespace huggorm {

// `Evaluator::wrap` answers one and is declared before `Bridge` is
// defined, so the name has to exist first. It sat in `gc.hpp` after
// the split, where nothing used it.
class Bridge;

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

// `EvalState::addPrimOp` is private too (`eval.hh:837`), so the same
// rule reaches it. A member FUNCTION this time rather than a data
// member, which `auto Member` already covers - the shim needed no
// change to take one.
struct AddPrimOp
{
};
auto get(AddPrimOp);

template struct Reach<AddPrimOp, &nix::EvalState::addPrimOp>;

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
    EvalCore(const std::string & store_uri, const Settings & settings,
             const std::optional<std::string> & build_store_uri)
        : store_uri_(store_uri)
        , eval_settings_(read_only_)
        , configured_(apply_configured(fetch_settings_, eval_settings_, settings))
        , store_(nix::openStore(store_uri))
        , build_store_(build_store_uri ? nix::openStore(*build_store_uri).get_ptr() : nullptr)
        , state_(nix::LookupPath{}, store_, fetch_settings_, eval_settings_, build_store_)
    {
    }

    EvalCore(const EvalCore &) = delete;
    EvalCore & operator=(const EvalCore &) = delete;

    nix::EvalState & state() { return state_; }

    const std::string & store_uri() const { return store_uri_; }

    // -- the ONE strong reference to each registered callable ------
    //
    // A primop's `impl` used to capture the `nb::object` itself, and
    // that made an UNCOLLECTABLE cycle: the callable is normally a
    // closure over the state that registered it - `state.make_int` is
    // how a primop builds its result - so the state reached the
    // callable through nix's base env, and the callable reached the
    // state through its closure cell. Python's collector cannot walk
    // the first arm, which lives in C++ memory it knows nothing
    // about. Measured in `tasks/093`: two `gc.collect()` calls did
    // not free it.
    //
    // So the reference lives HERE, in one place a `tp_traverse` slot
    // can report and a `tp_clear` slot can drop, and the lambda
    // carries an INDEX instead. The `weak_ptr` it already held is
    // what it looks the index up through.
    //
    // `release` empties each slot rather than the vector, because an
    // index handed to a lambda has to stay valid: a cleared slot is
    // an empty `nb::object` the lambda refuses on, and a shorter
    // vector would be an out-of-range read.
    //
    // EVERY READER AND WRITER OF `primops_` HOLDS THE GIL, and that
    // is the invariant this vector is safe under rather than a
    // remark. It is written on the state's affine thread and READ by
    // `tp_traverse` on whichever thread runs a collection, so a
    // `push_back` racing an iteration would reallocate under it.
    //
    // Nothing here takes a lock, because nothing needs one:
    //
    //   hold_primop     the emitted `register_primop` binding takes
    //                   NO `nb::call_guard<nb::gil_scoped_release>`,
    //                   unlike `subscribe_logs` beside it - checked
    //                   in the emitted `eval.cpp`, not assumed
    //   primop          the lambda reads after `gil_scoped_acquire`
    //   tp_traverse     the collector holds it
    //   release_primops `tp_clear`, likewise
    //   ~EvalCore       acquires it, and is the last owner anyway
    //
    // So giving `register_primop` a release guard - which `blocking`
    // decides - would be a data race, not a speed-up. Registration is
    // a vector push and a map insert; there is nothing to release for.

    std::size_t hold_primop(nb::object fn)
    {
        primops_.push_back(std::move(fn));
        return primops_.size() - 1;
    }

    /** The callable at `slot`, or an empty object once cleared. */
    nb::object primop(std::size_t slot) const
    {
        return slot < primops_.size() ? primops_[slot] : nb::object();
    }

    const std::vector<nb::object> & primops() const { return primops_; }

    void release_primops()
    {
        for (auto & fn : primops_)
            fn.reset();
    }

    /**
     * Drops the callables with the GIL HELD, before anything else.
     *
     * An `nb::object` going out of scope decrements a Python
     * refcount, and this destructor runs wherever the last share of
     * the core is dropped - `make_core`'s deleter exists because that
     * can be a pool worker, the server's reaper or a Python
     * finalizer. Only the last of those holds the GIL.
     *
     * A destructor BODY runs before any member is destroyed, so this
     * also removes the member-order question: the vector is empty by
     * the time its own destructor runs, whatever position it holds.
     *
     * `gil_scoped_acquire` is re-entrant, so the common case - a
     * Python finalizer that already holds it - costs a counter.
     */
    ~EvalCore()
    {
        if (primops_.empty())
            return;
        nb::gil_scoped_acquire gil;
        primops_.clear();
    }

private:
    std::string store_uri_;
    bool read_only_ = false;
    nix::fetchers::Settings fetch_settings_;
    nix::EvalSettings eval_settings_;
    // Between the settings and the state: nix.conf has to reach the
    // settings before `state_` reads them.
    bool configured_;
    nix::ref<nix::Store> store_;
    // Null when the state builds where it evaluates, which is what
    // `EvalState` itself takes a null to mean.
    std::shared_ptr<nix::Store> build_store_;
    nix::EvalState state_;
    std::vector<nb::object> primops_;
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
inline std::shared_ptr<EvalCore> make_core(const std::string & store_uri, const Settings & settings,
                                           const std::optional<std::string> & build_store_uri)
{
    return {new EvalCore(store_uri, settings, build_store_uri), [](EvalCore * core) {
                gc_register_thread();
                delete core;
            }};
}

class Evaluator
{
public:
    explicit Evaluator(const std::string & store_uri, const std::optional<Settings> & settings = std::nullopt,
                       const std::optional<std::string> & build_store_uri = std::nullopt)
        : core_(make_core(store_uri, settings.value_or(Settings{}), build_store_uri))
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

    /**
     * Publishes a Python callable as `builtins.<name>`.
     *
     * A MEMBER rather than a free function only because `core_` is
     * private and a `Bridge` needs it. Nothing else about it belongs
     * to the class.
     *
     * The full argument is beside the definition, below `Bridge`.
     */
    void register_primop(const std::string & name, std::size_t arity,
                         nb::object fn) const;

    // What the GC slots below report and drop. On the CORE rather
    // than here, because a Bridge shares the core and the callables
    // have to outlive this wrapper exactly as the state does.
    const std::vector<nb::object> & primops() const
    {
        return core_->primops();
    }

    void release_primops() const { core_->release_primops(); }

private:

    std::shared_ptr<EvalCore> core_;
};

// ---- letting Python's collector see the callables ------------------
//
// A HELPER the EMITTER BINDS TO. The declaration says
// `@gc_slots("huggorm::evaluator_slots")` and the generated
// `nb::class_` names this table; nothing here resolves a Python name,
// and generated code is the only caller.
//
// nanobind offers no abstraction for this - its own `refleaks.rst`
// says so and says to drop to the CPython slots, which is what these
// are. Read there rather than recalled, after `tasks/093` measured
// the leak.
//
// A declaration cannot carry it. A traversal is a FUNCTION the
// interpreter calls during collection, over a member the declaration
// does not know exists, and the DSL has no way to say "visit this".

/**
 * Report the callables, so a cycle through them is visible.
 *
 * `Py_VISIT(Py_TYPE(self))` first: an instance depends on its type,
 * and nanobind's own example starts there.
 *
 * The readiness check is not defensive noise. A traversal can run
 * after `__new__` and before the C++ constructor finished, and
 * reading the object then is reading uninitialised memory.
 *
 * A `const &` and no `nb::object` copy, deliberately: nanobind's doc
 * changed its own example for this, because under free-threading a
 * traversal that takes a reference to what it visits is wrong.
 */
inline int evaluator_tp_traverse(PyObject * self, visitproc visit, void * arg)
{
    Py_VISIT(Py_TYPE(self));
    if (!nb::inst_ready(self))
        return 0;
    for (const auto & fn : nb::inst_ptr<Evaluator>(self)->primops())
        Py_VISIT(fn.ptr());
    return 0;
}

/**
 * Break the cycle, by dropping the one reference that closes it.
 *
 * Python calls this only for an object it has already proved
 * unreachable, so releasing the callables cannot strand a caller.
 *
 * NO GATE DRIVES THIS, and it was measured rather than assumed.
 * Disabling this function changes nothing: the suite still passes and
 * nothing leaks. The reason is CPython's rule, which nanobind's own
 * doc states - every type in a cycle needs `tp_traverse`, and only
 * ONE of them needs `tp_clear`. The other participants here are a
 * function object and a cell, and both carry their own.
 *
 * It stays because that is a fact about THIS cycle, not about the
 * class. An `EvalState` reached only through C++ participants would
 * have nothing else to clear. Kept as correctness, and recorded as
 * untested (`tasks/093`).
 */
inline int evaluator_tp_clear(PyObject * self)
{
    nb::inst_ptr<Evaluator>(self)->release_primops();
    return 0;
}

inline PyType_Slot evaluator_slots[] = {
    {Py_tp_traverse, reinterpret_cast<void *>(evaluator_tp_traverse)},
    {Py_tp_clear, reinterpret_cast<void *>(evaluator_tp_clear)},
    {0, nullptr},
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

/**
 * Publishes a Python callable as `builtins.<name>`.
 *
 * The direction everything else here runs the other way. Every
 * emitted binding is Python calling C++; this is C++ calling Python,
 * in the middle of an evaluation, on Nix's thread. It is the first
 * such path since the mock went (`tasks/060` deleted the last
 * trampoline), and the fact it carries - a foreign evaluator's
 * callback re-entering the interpreter - is one no declaration can
 * express. Generated code CALLS it, which is what makes it a helper.
 *
 * WEAK, not shared. A `Bridge` needs the `EvalCore` that owns this
 * very state, so a callback capturing a share would make the state
 * own a callback that owns the state. Nothing would free either, and
 * `live_roots()` would not see it because no ROOT leaks. An expired
 * lock means the state is being destroyed, and a primop of a
 * destroyed state cannot be running - so that branch is unreachable
 * rather than merely unlikely, and it says so if it ever fires.
 *
 * FORCED BEFORE THE GIL. A primop receives thunks, and forcing one is
 * arbitrary evaluation - it can import files and call other primops.
 * Doing that while holding the GIL would stall every other Python
 * thread for the length of it, which is the exact cost every emitted
 * binding releases the GIL to avoid.
 *
 * Arguments cross as `Bridge`, which ROOTS each one, so a Python
 * object outliving the call retains its argument rather than
 * dangling. `tasks/033` argued for a borrowed view that refuses to
 * outlive the call; the root is why none is needed.
 *
 * The callable is NEVER RELEASED. `addPrimOp` does `new PrimOp(...)`
 * into GC memory and Boehm runs no destructors, so the captured
 * `nb::object` keeps its reference for the life of the process. A
 * registration is permanent, and that is upstream's shape rather
 * than a choice here.
 *
 * Errors go out the way `primops.cc` sends them
 * (`primops.cc:481`) - `.atPos(pos)`, because the position is the
 * half only a primop knows, and a Python failure with no position
 * points at the whole file.
 */
inline void Evaluator::register_primop(const std::string & name,
                                       std::size_t arity,
                                       nb::object fn) const
{
    std::weak_ptr<EvalCore> weak = core_;
    // The core OWNS the callable and the lambda carries an index, so
    // the only strong Python reference is one a `tp_clear` slot can
    // drop. Capturing `fn` here instead is what made the cycle in
    // `tasks/093`, and no `weak_ptr` fixes that: the arm that closes
    // it runs from C++ memory into Python, which is the direction
    // Python's collector cannot follow.
    const std::size_t slot = core_->hold_primop(std::move(fn));
    nix::PrimOp op{
        .name = name,
        // `args` stays empty on purpose. Upstream computes `arity`
        // from it when it is set, and names there are for
        // documentation this binding does not carry.
        .arity = arity,
        .impl = [weak, slot, arity, name](nix::EvalState & state,
                                          const nix::PosIdx pos,
                                          nix::Value ** args,
                                          nix::Value & out) {
            auto held = weak.lock();
            if (!held)
                state.error<nix::EvalError>("the evaluator is gone")
                    .atPos(pos)
                    .debugThrow();

            for (std::size_t i = 0; i < arity; ++i)
                state.forceValue(*args[i], pos);

            nb::gil_scoped_acquire gil;
            // Empty once `tp_clear` has run, which happens only for a
            // cycle Python already found unreachable - so nothing
            // should be able to call this. It is checked rather than
            // assumed, because the alternative is calling a null.
            nb::object fn = held->primop(slot);
            if (!fn.is_valid())
                state
                    .error<nix::EvalError>(
                        "the Python implementation of builtins.%1% was "
                        "released", name)
                    .atPos(pos)
                    .debugThrow();
            try {
                nb::list made;
                for (std::size_t i = 0; i < arity; ++i)
                    made.append(nb::cast(Bridge(held, args[i])));
                out = *nb::cast<Bridge>(fn(*nb::tuple(made))).get();
            } catch (nb::cast_error &) {
                // A `cast_error` and not a `python_error`: returning a
                // plain int fails HERE, and catching only the latter
                // would let it escape through C++ evaluation frames.
                state
                    .error<nix::EvalError>(
                        "the Python implementation of builtins.%1% did not "
                        "return a Value",
                        name)
                    .atPos(pos)
                    .debugThrow();
            } catch (nb::python_error & e) {
                state.error<nix::EvalError>("%1%", e.what())
                    .atPos(pos)
                    .debugThrow();
            }
        },
    };
    auto & state = core_->state();
    (state.*get(AddPrimOp{}))(std::move(op));

    // SORT, or the primop is registered and cannot be found.
    //
    // `addPrimOp` APPENDS to both structures and sorts neither.
    // Upstream gets away with it because `createBaseEnv` sorts once
    // when it has added them all, and says why (`primops.cc:5449`):
    //
    //     /* Now that we've added all primops, sort the `builtins'
    //        set, because attribute lookups expect it to be sorted. */
    //
    // Registering on a LIVE state runs after that, so both containers
    // are left out of order and an attribute lookup binary-searches
    // past the new name.
    //
    // Measured, not reasoned about. Without this, one gate failed and
    // six passed - the six by luck, because a freshly interned symbol
    // usually sorts last anyway. The failure is the shape worth
    // recognising:
    //
    //     error: attribute 'wrong' missing
    //            Did you mean wrong?
    //
    // The lookup missed and the SUGGESTION engine found it, because
    // one binary-searches and the other scans. A binding that shipped
    // without this would work almost always.
    const_cast<nix::Bindings *>(state.getBuiltins().attrs())->sort();
    state.staticBaseEnv->sort();
}

// ---- Bridge, out of line ------------------------------------------

}  // namespace huggorm
