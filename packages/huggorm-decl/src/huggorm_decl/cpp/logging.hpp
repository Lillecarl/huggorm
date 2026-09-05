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
    // "msg" | "start" | "stop" | "result" | "finalized".
    std::string action;
    uint64_t level = 0;
    uint64_t id = 0;
    uint64_t parent = 0;
    uint64_t type = 0;
    // The call this record was raised inside, or 0. See
    // `thread_request` below for what 0 means.
    uint64_t request = 0;
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
 * A full queue refuses a "msg" and a "result", and nothing else. A
 * dropped stop leaks a node in the reader's activity tree forever,
 * because nothing later says that activity ended - so the cost of
 * dropping is not the same for the two kinds, and one bound for both
 * would be the wrong answer for one of them. Activities are bounded
 * by the evaluation itself, so keeping them all is affordable in a
 * way that keeping every build log line is not.
 *
 * The test names what to DROP, not what to keep, and that is load
 * bearing rather than a style. `"finalized"` was added later and is a
 * CONTROL event: a lost one parks a reader that is waiting for a
 * call to end, so it may never be dropped. It inherited the
 * guarantee because this test enumerates the droppable set. The same
 * test written the other way round would have made it droppable in
 * silence.
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
 * The call this thread is inside, or 0.
 *
 * A `thread_local` for `thread_queue`'s reason and one more. Nix
 * raises a record on the thread that is working, so the call is
 * thread CONTEXT rather than logger state - and the push path runs
 * inside evaluation, where a map keyed by thread id would take a lock
 * per log line.
 *
 * ZERO is honest and not a gap. A record raised by a fetcher thread,
 * a file-transfer thread or a build belongs to no call this binding
 * made, and says so. Only the wrapped-call chokepoint in
 * `runtime.py` ever writes this.
 */
inline uint64_t & thread_request()
{
    static thread_local uint64_t request = 0;
    return request;
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
 * One record to at most one queue. True if a queue took it.
 *
 * A FALLBACK and not a broadcast, which is the whole decision. A
 * thread that subscribed CLAIMS its records, so a state's subscriber
 * sees what it saw before this existed and the process-wide queue
 * never repeats it. Broadcasting to both was the alternative: it
 * would let one process-wide reader see everything, at the cost of a
 * caller holding both subscriptions seeing every evaluation record
 * twice, with nothing on a record to deduplicate by.
 *
 * So "everything in this process" is NOT what the process-wide queue
 * answers. It answers what nobody else claimed, and the shape that
 * answers the other question is fan-out over one subscription, which
 * `server.py` now has.
 *
 * FREE rather than a private static of `LogTap`, which is where it
 * lived until `tasks/089`. `end_request` pushes the finalized marker
 * through it, and that marker has to land in the same queue the
 * call's records did - so the choice of queue cannot belong to the
 * logger.
 *
 * The STAMP is here for the same reason: one place decides which
 * queue takes a record, so one place is where every record gets its
 * call. An override that built a record without it would be a record
 * that quietly belongs to no call.
 */
inline bool route(LogRecord && r)
{
    r.request = thread_request();
    if (auto & queue = thread_queue()) {
        queue->push(std::move(r));
        return true;
    }
    if (auto queue = process_queue()) {
        queue->push(std::move(r));
        return true;
    }
    return false;
}

/**
 * The `nix::Logger` that fills the queues.
 *
 * Five record overrides, one per virtual, and each one builds the
 * record `JSONLogger` would have written and routes it. There is no
 * state here: the routing is the thread's, so one tap serves every
 * thread and every subscription.
 *
 * Two more overrides answer for the logger it REPLACED, because it
 * replaces rather than tees: `writeToStdout` keeps descriptor 1
 * clean, and `isVerbose` keeps a failed build's message the shape it
 * had.
 */
class LogTap : public nix::Logger
{
public:
    /**
     * Every override has the same two lines: route, and forward what
     * nobody took.
     *
     * The ORIGINAL arguments go to the fallback, never the record
     * built from them. `SimpleLogger::result` prints a
     * `resBuildLogLine` from `fields[0].s` (logging.cc:140), so a
     * fallback fed a `LogRecord` would have to rebuild `Fields` from
     * `LogField` to say the same thing. Forwarding the arguments
     * says it with no reconstruction, and the fallback's own
     * `nix::verbosity` gate stays exactly where nix put it.
     */
    void log(nix::Verbosity lvl, std::string_view s) override
    {
        if (!route({.action = "msg",
                    .level = static_cast<uint64_t>(lvl),
                    .text = std::string(s)}))
            fallback().log(lvl, s);
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
        if (!route({.action = "msg",
                    .level = static_cast<uint64_t>(ei.level),
                    .text = rendered.str()}))
            fallback().logEI(ei);
    }

    void startActivity(nix::ActivityId act, nix::Verbosity lvl,
                       nix::ActivityType type, const std::string & s,
                       const Fields & fields, nix::ActivityId parent) override
    {
        if (!route({.action = "start",
                    .level = static_cast<uint64_t>(lvl),
                    .id = act,
                    .parent = parent,
                    .type = static_cast<uint64_t>(type),
                    .text = s,
                    .fields = convert(fields)}))
            fallback().startActivity(act, lvl, type, s, fields, parent);
    }

    void stopActivity(nix::ActivityId act) override
    {
        if (!route({.action = "stop", .id = act}))
            fallback().stopActivity(act);
    }

    void result(nix::ActivityId act, nix::ResultType type,
                const Fields & fields) override
    {
        if (!route({.action = "result",
                    .id = act,
                    .type = static_cast<uint64_t>(type),
                    .fields = convert(fields)}))
            fallback().result(act, type, fields);
    }

    /**
     * Descriptor 1 is the protocol's, never a log's.
     *
     * `Logger::writeToStdout` writes descriptor 1 directly
     * (logging.cc:42), and `tasks/014` wants this protocol to run
     * over stdin/stdout. One stray line there is a corrupt frame,
     * not a stray line. So the tap sends it where every other record
     * goes.
     *
     * No caller reaches this in-process - every `cout` in 2.34.8 is
     * in `libcmd`, `libmain` or `src/nix`, and none of those is
     * linked here - so no gate can drive it from Python. It is a
     * guarantee about a descriptor, not a behaviour under test.
     *
     * A do-nothing override was the alternative, and it drops. This
     * one keeps the text at the level nix gives an unlabelled
     * message, `lvlInfo` (`logging.hh:136`).
     */
    void writeToStdout(std::string_view s) override
    {
        log(nix::lvlInfo, s);
    }

    /**
     * True, because the tap forwards every build log line.
     *
     * `derivation-building-goal.cc:1090` reads this to decide
     * whether a failed build's error needs the log tail appended.
     * The tee answered with `SimpleLogger(true)`'s `true`, and the
     * base class answers `false`, so leaving it alone would change a
     * build failure's message as a side effect of replacing a
     * logger. Every `resBuildLogLine` still reaches a queue or the
     * fallback, so `true` is also the honest answer.
     */
    bool isVerbose() override
    {
        return true;
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
     * Where a record goes when no queue claimed it.
     *
     * The logger nix itself installs at static init
     * (`logging.cc:35`), built here a second time because
     * `install_log_tap` no longer keeps the first one. It writes
     * descriptor 2 and gates on `nix::verbosity`, so an unsubscribed
     * caller sees exactly what it saw before the tap existed.
     *
     * NO SECOND CEILING. A `level <= lvlWarn` cut was written into
     * `tasks/089` first, on the argument that a library must not
     * narrate uninvited. The probe refuted it: at the default
     * verbosity nothing above `lvlWarn` reaches stderr anyway, so
     * the cut's only live effect is to silence a caller who RAISED
     * `nix::verbosity` - a silent drop, and this repo's named
     * failure mode.
     *
     * LEAKED, and deliberately. A `static unique_ptr` here destructs
     * at exit in an order nothing states, and a detached fetcher
     * thread that logs after that would use a destroyed object.
     * Nothing frees it and nothing needs to: one logger, for the
     * life of the process.
     */
    static nix::Logger & fallback()
    {
        static nix::Logger * simple = nix::makeSimpleLogger(true).release();
        return *simple;
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
 * A REPLACEMENT, and it was a tee until now. `makeTeeLogger` keeps
 * the logger that was already there as the MAIN one, so every record
 * reached stderr whether a subscriber took it or not. A client
 * reading this protocol over stdin/stdout cannot have that: the tee
 * writes descriptor 2 always and descriptor 1 on `cout`, and neither
 * belongs to it.
 *
 * Nothing is lost by dropping the tee, because `LogTap::fallback`
 * builds the same `SimpleLogger` and uses it for what no queue took.
 * The one behaviour that changes is the one Carl asked for: a
 * SUBSCRIBED caller no longer also gets the record on stderr.
 */
inline void install_log_tap()
{
    nix::logger = std::make_unique<LogTap>();
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
