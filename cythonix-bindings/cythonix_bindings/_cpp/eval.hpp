#pragma once

/**
 * The one thing a binding over a GC-managed value has to do itself.
 *
 * fake_library::Value lives in the collector's heap and belongs to
 * nobody. It stays alive for exactly as long as the collector can SEE
 * a pointer to it, and the collector scans registered thread stacks
 * and its own heap - not Python's. So a Python object holding a bare
 * `Value *` roots nothing, and the value it names can be collected
 * while the wrapper is still being used.
 *
 * A Bridge is the fix and it is one field: a cell allocated with
 * GC_malloc_uncollectable, which is memory the collector never
 * reclaims and always SCANS. The pointer inside it is therefore a
 * root. The cell lives exactly as long as the Bridge, so the value
 * becomes reclaimable the moment Python drops the wrapper - even
 * while its EvalState lives on, which is the lifetime libexpr
 * documents.
 *
 * The cell belongs to the Bridge, not to the binding. Its
 * constructor takes it and its destructor frees it, so the lifetime
 * is attached to one object rather than to two methods that have to
 * agree - and a declaration says `cxx="cythonix::Bridge"` and needs
 * to know none of it.
 */

#include <cstddef>
#include <cstdint>
#include <string>

// eval.hpp first: it pulls in gc-env.hpp, which defines GC_THREADS
// before gc.h is ever included. Reversed, gc.h processes without
// thread support and its include guard hides the registration API
// from every later consumer.
#include "fake_library/eval.hpp"

#include <gc/gc.h>

namespace cythonix {

class Bridge
{
public:
    /**
     * Registers the calling thread, then roots `value`.
     *
     * The registration is not optional and it is not the caller's to
     * remember: allocating from a thread the collector does not know
     * races with a collection. It is idempotent, and it is here rather
     * than at each call site because the allocation below is the only
     * reason it is needed.
     */
    explicit Bridge(fake_library::Value * value)
    {
        fake_library::gcenv::register_current_thread();
        cell_ = static_cast<fake_library::Value **>(
            GC_malloc_uncollectable(sizeof(fake_library::Value *)));
        cell_[0] = value;
    }

    /** A copy is a second root over the same value. */
    Bridge(const Bridge & other) : Bridge(other.cell_[0]) {}

    Bridge & operator=(const Bridge & other)
    {
        if (this != &other)
            cell_[0] = other.cell_[0];
        return *this;
    }

    /**
     * Frees the cell, which drops the last visible reference.
     *
     * This runs on WHICHEVER thread drops the last Python reference,
     * and that is rarely the thread that produced the value: an event
     * loop, a pool worker, the server's reaper. GC_free takes the
     * collector's lock and may have to cooperate with a collection, so
     * the thread has to be registered first. Reading a value needs no
     * registration - the cell keeps it reachable from anywhere - but
     * freeing does.
     */
    ~Bridge()
    {
        if (cell_ != nullptr) {
            fake_library::gcenv::register_current_thread();
            GC_free(cell_);
        }
    }

    fake_library::Value * get() const { return cell_[0]; }

    /**
     * The underlying value's address, as a number.
     *
     * What makes two wrappers THE SAME node. A fresh wrapper is built
     * for every access, so Python identity says nothing: two wrappers
     * over one value differ, and a wrapper that dies hands its id()
     * to the next one. Only this file knows where the identity lives.
     */
    std::uintptr_t identity() const
    {
        return reinterpret_cast<std::uintptr_t>(cell_[0]);
    }

    /**
     * Whether this value sits inside a GC-allocated block.
     *
     * Bound straight from gc.h: a no-op integration cannot fake it.
     */
    bool is_gc_managed() const { return GC_base(cell_[0]) != nullptr; }

private:
    fake_library::Value ** cell_ = nullptr;
};

/** Live collector counters, straight from gc.h. */
inline std::size_t gc_heap_size() { return GC_get_heap_size(); }
inline std::size_t gc_total_bytes() { return GC_get_total_bytes(); }
inline std::size_t gc_free_bytes() { return GC_get_free_bytes(); }
inline std::size_t gc_bytes_since_gc() { return GC_get_bytes_since_gc(); }
inline std::size_t gc_collections()
{
    return static_cast<std::size_t>(GC_get_gc_no());
}

}  // namespace cythonix
