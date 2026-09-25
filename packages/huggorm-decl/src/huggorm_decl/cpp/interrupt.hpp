#pragma once

/**
 * Cancelling one call, from another thread.
 *
 * libutil stops work at `checkInterrupt`, which asks two things: the
 * process-wide `_isInterrupted` and a thread-local hook,
 * `nix::unix::interruptCheck`. The process-wide flag is what a SIGINT
 * sets and nothing clears, so it would stop every call and keep them
 * stopped. The hook is per thread, and it is the one used here.
 *
 * The hook cannot be set per call from the thread that cancels, because
 * it belongs to the thread doing the work. So it is installed once on
 * each thread that enters a call, and it asks this table whether the
 * call the thread is in (`thread_request`) was cancelled. The cancelling
 * side only writes the table.
 *
 * `checkInterrupt` runs on hot paths, so the hook reads an atomic count
 * first and takes the lock only while some cancellation is pending.
 *
 * Cancellation takes effect at the next `checkInterrupt`. A blocking
 * system call runs to its end first.
 */

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <mutex>
#include <unordered_set>

#include "nix/util/signals.hh"

#include "huggorm_decl/cpp/logging.hpp"

namespace huggorm {

class Cancellations
{
public:
    void cancel(std::uint64_t request)
    {
        std::lock_guard guard(lock_);
        if (ids_.insert(request).second)
            pending_.fetch_add(1, std::memory_order_release);
    }

    void forget(std::uint64_t request)
    {
        std::lock_guard guard(lock_);
        if (ids_.erase(request))
            pending_.fetch_sub(1, std::memory_order_release);
    }

    bool cancelled(std::uint64_t request)
    {
        if (request == 0 || pending_.load(std::memory_order_acquire) == 0)
            return false;
        std::lock_guard guard(lock_);
        return ids_.contains(request);
    }

private:
    std::mutex lock_;
    std::unordered_set<std::uint64_t> ids_;
    std::atomic<std::size_t> pending_{0};
};

inline Cancellations & cancellations()
{
    static Cancellations table;
    return table;
}

/**
 * Put the hook on the calling thread, once.
 *
 * A hook already there is kept and asked too, so an embedder's own
 * interrupt still works on a thread that also runs huggorm calls.
 */
inline void install_interrupt_check()
{
    thread_local bool installed = false;
    if (installed)
        return;
    installed = true;
    auto previous = std::move(nix::unix::interruptCheck);
    nix::unix::interruptCheck = [previous = std::move(previous)] {
        return cancellations().cancelled(thread_request()) || (previous && previous());
    };
}

}  // namespace huggorm
