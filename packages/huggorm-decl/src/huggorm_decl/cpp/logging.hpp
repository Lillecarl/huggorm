#pragma once

/**
 * The log tap, and the queues it fills.
 *
 * Split out of `eval.hpp`, which had grown to hold four unrelated
 * things. Nothing here knows what an `EvalState` is: the tap receives
 * from nix and routes to a queue, and every accessor Python sees comes
 * from `@reads` in the declaration.
 *
 * A HELPER by this repo's own test - it is a callback receiver over a
 * foreign library, and generated code calls it. `LogRecord` and
 * `LogField` have no method at all.
 */

#include <cstddef>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include "nix/util/error.hh"
#include "nix/util/logging.hh"

namespace huggorm {

// ---- the log tap --------------------------------------------------
//
// The other direction, again. `register_primop` lets nix call a
// Python function; this lets nix TELL Python what it is doing, and
// neither is a call Python made.
//
// A HELPER by the same test as `Bridge` and `cached_files`: it is a
// callback receiver over a foreign library, and generated code calls
// it. `LogRecord` and `LogField` have no method at all - every
// accessor Python sees comes from `@reads` in the declaration, so the
// mapping is still written by the emitter.
//
// The one part no declaration can carry today is `LogTap` itself.
// `nix::Logger` is ABSTRACT, so a tap is a SUBCLASS, and the DSL can
// say "Python may not construct one of these" (`@abstract`) but
// cannot say "implement these virtuals". `tasks/084` is that gap.

/**
 * One field of one record.
 *
 * Mirrors `nix::Logger::Field`, whose own FIXME asks for a
 * `std::variant` (logging.hh:76). Copying the shape upstream has
 * keeps this honest: when that FIXME is taken, one struct changes
 * here and the declaration follows it.
 */
struct LogField
{
    bool is_int = false;
    uint64_t integer = 0;
    std::string text;
};

/**
 * One record, in Nix's own vocabulary.
 *
 * The field names are `JSONLogger`'s (logging.cc:272-333), because a
 * client that already reads `--log-format internal-json` should not
 * have to learn a second spelling of the same record. Not the same
 * MECHANISM - see `tasks/032` for why the JSON logger was rejected -
 * but the same shape.
 *
 * `level`, `type` and `id` cross as integers. They ARE integers:
 * `Verbosity`, `ActivityType` and `ResultType` are int-valued C++
 * enums, and nothing upstream parses one from a string. `decl/words.py`
 * exists for vocabularies where a member IS the string libstore
 * parses, and forcing these in would make that file's rationale
 * false.
 */
struct LogRecord
{
    // "msg" | "start" | "stop" | "result".
    std::string action;
    uint64_t level = 0;
    uint64_t id = 0;
    uint64_t parent = 0;
    uint64_t type = 0;
    std::string text;
    std::vector<LogField> fields;
};

/**
 * A bounded queue one subscriber drains.
 *
 * A QUEUE and not a callback, and that is the whole design. A record
 * is raised on whichever thread is working - the evaluation thread,
 * mid-evaluation - and a callback there would take the GIL once per
 * record and run arbitrary Python inside the evaluator. `push` takes
 * a mutex, copies, and returns.
 *
 * The DROP POLICY is the interesting part of a bounded queue.
 *
 * A full queue refuses a "msg" and a "result", and NEVER a "start" or
 * a "stop". A dropped stop leaks a node in the reader's activity tree
 * forever, because nothing later says that activity ended - so the
 * cost of dropping is not the same for the two kinds, and one bound
 * for both would be the wrong answer for one of them. Activities are
 * bounded by the evaluation itself, so keeping them all is affordable
 * in a way that keeping every build log line is not.
 *
 * `level` filters a "msg" ONLY. Filtering a "start" by level would
 * leak a node the same way dropping one does, and upstream never
 * filters activities either: `Activity::Activity` calls
 * `startActivity` with no test (logging.cc:196), and the `lvl` it
 * passes is a field of the record rather than a gate.
 *
 * The global `nix::verbosity` still filters BEFORE any logger runs
 * (logging.hh:314, :330), so this level can only narrow. Asking for
 * more than the global gets nothing.
 */
class LogQueue
{
public:
    LogQueue(std::size_t capacity, uint64_t level)
        : capacity_(capacity)
        , level_(level)
    {
    }

    LogQueue(const LogQueue &) = delete;
    LogQueue & operator=(const LogQueue &) = delete;

    /** Any thread. Never touches Python. */
    void push(LogRecord && r)
    {
        const bool droppable = r.action == "msg" || r.action == "result";
        std::lock_guard<std::mutex> held(mutex_);
        if (!open_)
            return;
        if (r.action == "msg" && r.level > level_)
            return;
        if (droppable && records_.size() >= capacity_) {
            ++dropped_;
            return;
        }
        records_.push_back(std::move(r));
    }

    /** Everything waiting, and the queue is empty afterwards. */
    std::vector<LogRecord> drain()
    {
        std::lock_guard<std::mutex> held(mutex_);
        std::vector<LogRecord> out(
            std::make_move_iterator(records_.begin()),
            std::make_move_iterator(records_.end()));
        records_.clear();
        return out;
    }

    /**
     * How many records the bound refused, over the queue's life.
     *
     * Cumulative rather than per-drain, so a reader that misses one
     * drain still sees the number grow. A reader that wants the
     * per-drain figure subtracts.
     */
    uint64_t dropped() const
    {
        std::lock_guard<std::mutex> held(mutex_);
        return dropped_;
    }

    /** Stop recording, and let go of what is waiting. */
    void close()
    {
        std::lock_guard<std::mutex> held(mutex_);
        open_ = false;
        records_.clear();
    }

private:
    mutable std::mutex mutex_;
    std::deque<LogRecord> records_;
    std::size_t capacity_;
    uint64_t level_;
    uint64_t dropped_ = 0;
    bool open_ = true;
};

/**
 * The queue this thread's records go to, if any.
 *
 * A `thread_local` rather than a map keyed by thread id, because the
 * push path runs inside evaluation and a lock there would be paid per
 * log line.
 *
 * A THREAD is the right key because an `EvalState` is affine and this
 * Nix evaluates on one thread: nothing under `src/libexpr` names
 * `eval-cores`, so 2.34.8 has no parallel evaluation and the thread
 * that owns a state is the thread its records are raised on.
 *
 * The gap this leaves is named rather than hidden. A record raised by
 * a fetcher or a file-transfer thread reaches no queue, because that
 * thread has none. A process-wide subscriber is what covers it, and
 * the rpc is what needs one.
 */
inline std::shared_ptr<LogQueue> & thread_queue()
{
    static thread_local std::shared_ptr<LogQueue> queue;
    return queue;
}

/**
 * The one slot for records raised on a thread that subscribed to none.
 *
 * The gap `thread_queue` names, closed. A fetcher thread, a
 * file-transfer thread and a build's own output all raise records on
 * threads no `EvalState` owns, so no `thread_local` reaches them - and
 * a build's log is what a Nix user most wants to see (`tasks/085`).
 *
 * A MUTEX here, where `thread_queue` needs none, and the reason the
 * thread_local's rationale does not transfer: that slot is read and
 * written by one thread, so `route` reads it lock-free. This slot is
 * read from every thread while another subscribes, which is a data
 * race on the `shared_ptr` itself - two words, non-atomically
 * updated. The cost is paid only by threads that have no queue of
 * their own, and `LogQueue::push` takes a mutex one line later
 * anyway.
 *
 * `std::atomic<std::shared_ptr<T>>` would do it lock-free and is not
 * used: it is free-standing-optional in libstdc++ and the lock here
 * is off the evaluation path entirely.
 */
inline std::pair<std::mutex, std::shared_ptr<LogQueue>> & process_sink()
{
    static std::pair<std::mutex, std::shared_ptr<LogQueue>> sink;
    return sink;
}

/** A share of the process-wide queue, or null. Any thread. */
inline std::shared_ptr<LogQueue> process_queue()
{
    auto & sink = process_sink();
    std::lock_guard<std::mutex> held(sink.first);
    return sink.second;
}

/**
 * The `nix::Logger` that fills the queues.
 *
 * Five overrides, one per virtual, and each one builds the record
 * `JSONLogger` would have written and routes it. There is no state
 * here: the routing is the thread's, so one tap serves every thread
 * and every subscription.
 */
class LogTap : public nix::Logger
{
public:
    void log(nix::Verbosity lvl, std::string_view s) override
    {
        route({.action = "msg",
               .level = static_cast<uint64_t>(lvl),
               .text = std::string(s)});
    }

    void logEI(const nix::ErrorInfo & ei) override
    {
        // RENDERED, the way JSONLogger renders it (logging.cc:283).
        // An ErrorInfo carries a trace of positions, and a record
        // that carried those parts would be a second error shape
        // beside the one the typed-status path already crosses with
        // (`tasks/036`). Those two should agree, and `tasks/032`
        // holds that question open rather than answering it twice.
        std::ostringstream rendered;
        nix::showErrorInfo(rendered, ei, nix::loggerSettings.showTrace.get());
        route({.action = "msg",
               .level = static_cast<uint64_t>(ei.level),
               .text = rendered.str()});
    }

    void startActivity(nix::ActivityId act, nix::Verbosity lvl,
                       nix::ActivityType type, const std::string & s,
                       const Fields & fields, nix::ActivityId parent) override
    {
        route({.action = "start",
               .level = static_cast<uint64_t>(lvl),
               .id = act,
               .parent = parent,
               .type = static_cast<uint64_t>(type),
               .text = s,
               .fields = convert(fields)});
    }

    void stopActivity(nix::ActivityId act) override
    {
        route({.action = "stop", .id = act});
    }

    void result(nix::ActivityId act, nix::ResultType type,
                const Fields & fields) override
    {
        route({.action = "result",
               .id = act,
               .type = static_cast<uint64_t>(type),
               .fields = convert(fields)});
    }

private:
    static std::vector<LogField> convert(const Fields & fields)
    {
        std::vector<LogField> out;
        out.reserve(fields.size());
        for (const auto & f : fields)
            out.push_back(f.type == nix::Logger::Field::tInt
                              ? LogField{.is_int = true, .integer = f.i}
                              : LogField{.is_int = false, .text = f.s});
        return out;
    }

    /**
     * One record to exactly one queue.
     *
     * A FALLBACK and not a broadcast, which is the whole decision. A
     * thread that subscribed CLAIMS its records, so a state's
     * subscriber sees what it saw before this existed and the
     * process-wide queue never repeats it. Broadcasting to both was
     * the alternative: it would let one process-wide reader see
     * everything, at the cost of a caller holding both subscriptions
     * seeing every evaluation record twice, with nothing on a record
     * to deduplicate by.
     *
     * So "everything in this process" is NOT what the process-wide
     * queue answers. It answers what nobody else claimed, and the
     * shape that would answer the other question is fan-out over one
     * subscription - `tasks/085`'s third gap, still open.
     */
    static void route(LogRecord && r)
    {
        if (auto & queue = thread_queue()) {
            queue->push(std::move(r));
            return;
        }
        if (auto queue = process_queue())
            queue->push(std::move(r));
    }
};

/**
 * Puts the tap behind the logger that is already there.
 *
 * ONCE, at import, beside `initGC`. Lazily on first subscribe would
 * replace `nix::logger` - a plain global `unique_ptr` (logging.hh:258)
 * - while another thread may be reading it, which is a race with no
 * lock to take.
 *
 * A TEE, so a console user still sees output: `makeTeeLogger` keeps
 * the logger that was installed as the MAIN one, which is the one it
 * uses for stdout and for asking the user a question. So this file
 * writes no forwarding of its own.
 */
inline void install_log_tap()
{
    std::vector<std::unique_ptr<nix::Logger>> extra;
    extra.push_back(std::make_unique<LogTap>());
    nix::logger = nix::makeTeeLogger(std::move(nix::logger), std::move(extra));
}

/**
 * Start recording this thread's records, and hand back the queue.
 *
 * Replaces whatever this thread was recording to, and CLOSES it: two
 * live subscriptions on one thread would each get an arbitrary half
 * of the records, which is worse than either getting none.
 */
inline std::shared_ptr<LogQueue> subscribe_logs(std::size_t capacity,
                                                uint64_t level)
{
    auto & slot = thread_queue();
    if (slot)
        slot->close();
    slot = std::make_shared<LogQueue>(capacity, level);
    return slot;
}

/** Stop recording. A queue already handed out still drains. */
inline void unsubscribe_logs()
{
    auto & slot = thread_queue();
    if (slot)
        slot->close();
    slot.reset();
}

/**
 * Start recording what no subscribed thread claims.
 *
 * REPLACES, exactly like `subscribe_logs`, and closes what it
 * replaced. Refusing was the other answer and it is the wrong one
 * here: an in-process caller that drops its `LogStream` without
 * unsubscribing would wedge the sink for the life of the process,
 * with nothing to take it back. Replacing self-heals.
 *
 * That leaves one shared stream with no owner, which is the scoping
 * question `tasks/032` opened. It is answered ABOVE this layer: the
 * rpc admits one process-logs stream at a time and refuses a second
 * with FAILED_PRECONDITION, the way `Session/Logs` already refuses a
 * second reader of one state.
 *
 * The old queue is closed OUTSIDE the sink's mutex. `close` takes the
 * queue's own mutex, and holding two locks in one order here and the
 * other order anywhere else is how a deadlock is built.
 */
inline std::shared_ptr<LogQueue> subscribe_process_logs(std::size_t capacity,
                                                        uint64_t level)
{
    auto fresh = std::make_shared<LogQueue>(capacity, level);
    std::shared_ptr<LogQueue> old;
    {
        auto & sink = process_sink();
        std::lock_guard<std::mutex> held(sink.first);
        old = std::exchange(sink.second, fresh);
    }
    if (old)
        old->close();
    return fresh;
}

/** Stop recording process-wide. A queue already handed out drains. */
inline void unsubscribe_process_logs()
{
    std::shared_ptr<LogQueue> old;
    {
        auto & sink = process_sink();
        std::lock_guard<std::mutex> held(sink.first);
        old = std::exchange(sink.second, {});
    }
    if (old)
        old->close();
}
}  // namespace huggorm
