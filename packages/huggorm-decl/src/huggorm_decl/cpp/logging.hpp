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

#include <array>
#include <atomic>
#include <cctype>
#include <cstddef>
#include <cstdlib>
#include <cstdint>
#include <deque>
#include <memory>
#include <mutex>
#include <span>
#include <sstream>
#include <stdexcept>
#include <string>
#include <string_view>
#include <variant>
#include <type_traits>
#include <utility>
#include <vector>

#include "huggorm_bindings/path_records.hpp"
#include "huggorm_decl/cpp/error_info.hpp"
#include "nix/store/remote-store.hh"
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
// cannot say "implement these virtuals". huggorm#84 is that gap.

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
 * MECHANISM - see huggorm#32 for why the JSON logger was rejected -
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
    // What `logEI` was given, beside `text`. Nothing else sets it.
    std::optional<ErrorInfo> info;
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
 * A QUEUE NO LONGER FILTERS BY LEVEL, and that is huggorm#89 step 4
 * removing a duplicate rather than a feature. It held a `level_` and
 * refused a "msg" above it, which was the only gate a subscriber had
 * while `nix::verbosity` decided everything else.
 *
 * `effective_verbosity` decides now, per thread, and it reaches every
 * record before any queue sees one. Nothing can arrive here that the
 * thread's level did not already admit:
 *
 *   - a subscribed thread has `subscribe_logs`'s level, set from the
 *     same number the queue used to hold;
 *   - a thread with no level of its own reads the process default,
 *     which `subscribe_process_logs` sets from ITS number - and a
 *     thread with its own level never reaches the process queue,
 *     because `route` gives its records to its own.
 *
 * So the two filters were one fact stated twice, and goal 3 says one
 * of them owns it. Keeping both hid a gate: removing the tap's test
 * left `test_the_same_work_at_the_default_says_nothing` PASSING,
 * because this filter caught what the tap let through.
 */
class LogQueue
{
public:
    explicit LogQueue(std::size_t capacity)
        : capacity_(capacity)
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
 * The pin on `nix::verbosity`, and the widest level a live
 * subscription asks the daemon for.
 *
 * TWO GATES, and they answer different questions:
 *
 *   nix::verbosity        will nix PRODUCE the record at all
 *   effective_verbosity   will this thread KEEP it
 *
 * The first is PINNED, once, at import (`install_log_tap`), at
 * `HUGGORM_LOG_CEILING`. `printMsg` reads it on every thread before
 * any logger runs (logging.hh:314), and it is a plain global, so any
 * write after a Nix thread exists is a data race. nanopynix-bindings
 * found that race with ThreadSanitizer. A subscription above the pin
 * gets nothing above it: nix never produces the record.
 *
 * THE DAEMON IS TOLD SOMETHING ELSE. `RemoteStore::setOptions` sends
 * `nix::remoteVerbosity` (nix-remote-verbosity.patch), and
 * `daemon.cc:239` assigns it to the daemon's own gate. The client
 * re-raises each daemon line with `printError`
 * (`worker-protocol-connection.cc:75`) and ERASES its level, so a
 * daemon `debug()` arrives as lvlError and passes every gate there
 * is. Sent the pin, every daemon narrates CHATTY lines onto an
 * unsubscribed caller's stderr (huggorm#95, huggorm#102).
 *
 * So this keeps the daemon level at the WIDEST level any live
 * subscription asks for, and at lvlInfo when none does. The pin does
 * not cap it: a debug subscriber gets the daemon's debug lines,
 * because they arrive as errors.
 *
 * A COUNT PER LEVEL rather than a maximum, because a maximum cannot
 * be undone: two subscriptions at talkative and one at debug, and
 * the debug one leaving has to return talkative rather than lvlInfo.
 *
 * ONE THING THIS CANNOT REACH: a daemon connection already open.
 * `setOptions` runs once, at handshake, so a `Store` opened while a
 * vomit subscription was live keeps receiving vomit until it is
 * closed (huggorm#96).
 */
class VerbosityDemand
{
public:
    void add(nix::Verbosity level)
    {
        std::lock_guard<std::mutex> held(mutex_);
        ++holders_[index(level)];
        reconcile();
    }

    void drop(nix::Verbosity level)
    {
        std::lock_guard<std::mutex> held(mutex_);
        auto & count = holders_[index(level)];
        if (count > 0)
            --count;
        reconcile();
    }

private:
    static std::size_t index(nix::Verbosity level)
    {
        const auto raw = static_cast<std::size_t>(level);
        return raw < kLevels ? raw : kLevels - 1;
    }

    /** Under `mutex_`. */
    void reconcile()
    {
        nix::Verbosity widest = nix::lvlInfo;
        for (std::size_t i = kLevels; i-- > 0;)
            if (holders_[i] > 0) {
                if (static_cast<nix::Verbosity>(i) > widest)
                    widest = static_cast<nix::Verbosity>(i);
                break;
            }
        nix::remoteVerbosity.store(std::to_underlying(widest), std::memory_order_relaxed);
    }

    static constexpr std::size_t kLevels = std::to_underlying(nix::lvlVomit) + 1;

    std::mutex mutex_;
    std::array<int, kLevels> holders_{};
};

/**
 * LEAKED, for `LogTap::fallback`'s reason. A thread that exits during
 * process teardown drops its level through `ThreadLevel`'s
 * destructor, and a function-local static destroyed before that
 * would be used after it was gone.
 */
inline VerbosityDemand & verbosity_demand()
{
    static VerbosityDemand * demand = new VerbosityDemand();
    return *demand;
}

/**
 * The level a thread with no level of its own REPORTS at.
 *
 * Not `nix::verbosity`, which is the pin: what nix PRODUCES, for the
 * whole process. This one is what a thread that asked for nothing
 * KEEPS, and it goes to nix's own default the moment the process sink
 * detaches.
 *
 * An ATOMIC, and it has to be: nix starts threads this binding never
 * sees - a curl worker, a substituter, a build hook reader - and none
 * of them passes anything that could set a thread level. They read
 * this instead, and they read it while another thread writes it.
 *
 * `lvlInfo` is nix's own default (`logging.cc:150`), so a caller who
 * asks for nothing sees what it saw before any of this existed.
 */
inline std::atomic<int> & default_verbosity()
{
    static std::atomic<int> level{static_cast<int>(std::to_underlying(nix::lvlInfo))};
    return level;
}

/**
 * What the process default asks nix to produce. -1 releases.
 *
 * `ThreadLevel::set` for the process, and the same rule: one place
 * owns the pairing of an add with the drop it replaces.
 *
 * `default_verbosity` cannot stand in for the number held here. It
 * reads `lvlInfo` both when nobody is subscribed and when a sink
 * asked for exactly `lvlInfo`, and a release has to give back what
 * was taken rather than a number that happens to match.
 *
 * A MUTEX rather than an atomic exchange, and that is not caution.
 * With an exchange, two concurrent subscribers race: B's exchange
 * reads A's level and drops it before A's add has landed, the
 * guarded decrement finds a zero and does nothing, and A's add then
 * has no owner - so the gate stays up after both unsubscribe. The
 * rpc admits one process-logs stream at a time, so it is
 * unreachable through the service; an in-process caller has no such
 * rule.
 *
 * ADD BEFORE DROP, so the gate never dips between a replacement and
 * what it replaces.
 *
 * Lock order: this one, then `VerbosityDemand`'s. Nothing takes them
 * the other way round.
 */
inline void set_process_demand(int wanted)
{
    static std::mutex mutex;
    static int held = -1;
    std::lock_guard<std::mutex> guard(mutex);
    if (wanted >= 0)
        verbosity_demand().add(static_cast<nix::Verbosity>(wanted));
    if (held >= 0)
        verbosity_demand().drop(static_cast<nix::Verbosity>(held));
    held = wanted;
}

/**
 * The level a thread with no level of its own reports at, and the
 * demand that makes nix produce it.
 *
 * One call for both, because the two are one fact. A default that
 * moved without its demand would name a level nix never produces, so
 * the tap would have nothing to keep.
 */
inline void set_default_verbosity(nix::Verbosity level)
{
    set_process_demand(static_cast<int>(level));
    default_verbosity().store(static_cast<int>(level),
                              std::memory_order_relaxed);
}

/**
 * What THIS thread asked for, and whether it asked at all.
 *
 * Two fields rather than one, and the flag is the load-bearing half.
 * A thread that never chose must follow the process default AS IT
 * CHANGES: `thread_local` initialisers run once, on first use, so a
 * fetcher thread that started before a caller raised the default
 * would keep the old value forever. `own` is what makes the default
 * live rather than a snapshot.
 *
 * A CLASS, and NOT ASSIGNABLE, and that is a fix rather than a
 * style. This was a plain aggregate with two public fields until
 * huggorm#96 gave it a destructor - so that a thread exiting while
 * still subscribed gives its level back. The two call sites then
 * read:
 *
 *     chosen = {.own = true, .level = wanted};
 *
 * which builds a TEMPORARY, copy-assigns it, and destroys the
 * temporary - running the new destructor on a copy that says it owns
 * the level, and dropping the demand that had just been added. Net
 * zero. Every per-thread subscription silently failed to move nix's
 * gate, and the suite said so in three gates at once.
 *
 * So the pairing lives in ONE place that owns both halves, and the
 * copy assignment that made the mistake possible is deleted. The
 * compiler now rejects the line that was wrong.
 */
class ThreadLevel
{
public:
    ThreadLevel() = default;
    ThreadLevel(const ThreadLevel &) = delete;
    ThreadLevel & operator=(const ThreadLevel &) = delete;

    bool own() const { return own_; }
    nix::Verbosity level() const { return level_; }

    /**
     * Ask for `level`, and give back whatever this thread held.
     *
     * ADD BEFORE DROP. A subscription replacing its own would
     * otherwise let nix's gate dip between the two calls, and a
     * record raised in that window is one nobody can get back.
     */
    void set(nix::Verbosity level)
    {
        verbosity_demand().add(level);
        release();
        own_ = true;
        level_ = level;
    }

    /** No opinion again, and the demand goes back. */
    void clear()
    {
        release();
    }

    /**
     * The last resort, and it fires only for a thread that exits
     * while still subscribed - one nix started and this binding
     * never sees. Without it that thread's level would hold nix's
     * gate up for the life of the process, which is the defect
     * huggorm#96 removes.
     */
    ~ThreadLevel()
    {
        release();
    }

private:
    void release()
    {
        if (!own_)
            return;
        own_ = false;
        verbosity_demand().drop(level_);
    }

    bool own_ = false;
    nix::Verbosity level_ = nix::lvlInfo;
};

inline ThreadLevel & thread_level()
{
    static thread_local ThreadLevel chosen;
    return chosen;
}

/**
 * The level that decides what this thread's records are worth.
 *
 * The only gate ON THIS SIDE, which is the whole of huggorm#89 step
 * 4. Nix produces everything up to the pin, and this decides who
 * keeps it - per thread, so one caller asking for talkative does not
 * put every other logger in the process on stderr.
 *
 * A relaxed load, because there is nothing to order against: the
 * value is one int and a reader that sees the previous one for a
 * moment prints one line differently.
 */
inline nix::Verbosity effective_verbosity()
{
    const auto & chosen = thread_level();
    if (chosen.own())
        return chosen.level();
    return static_cast<nix::Verbosity>(
        default_verbosity().load(std::memory_order_relaxed));
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
 * a build's log is what a Nix user most wants to see (huggorm#85).
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
 * lived until huggorm#89. `end_request` pushes the finalized marker
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

namespace detail {

template <typename L>
struct logger_fields
{
    using type = std::span<const typename L::Field>;
};

template <typename L>
    requires requires { typename L::Fields; }
struct logger_fields<L>
{
    using type = const typename L::Fields &;
};

}  // namespace detail

/**
 * How `nix::Logger` hands an override an activity's fields: a
 * `const Fields &` to 2.35, a `std::span` of variants from 2.36. An
 * override must spell its base's parameter exactly (huggorm#55).
 */
using LoggerFields = detail::logger_fields<nix::Logger>::type;

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
     * says it with no reconstruction.
     *
     * THE FALLBACK GATES ON THE WRONG LEVEL, and that is why every
     * override below tests `effective_verbosity()` before
     * forwarding. `SimpleLogger::log` reads `nix::verbosity`
     * (logging.cc:118), which `install_log_tap` pins at the ceiling
     * for the whole process, because nix has no per-thread gate of
     * its own. So forwarding unguarded would put every chatty line
     * on every caller's stderr. The tap does the filtering per thread
     * that nix's global cannot.
     *
     * A MESSAGE is gated before routing too. An ACTIVITY is not, and
     * the asymmetry is the same one `LogQueue::push` already makes:
     * a start that never arrives leaves a node in the reader's tree
     * that nothing closes.
     */
    void log(nix::Verbosity lvl, std::string_view s) noexcept override
    {
        if (lvl > effective_verbosity())
            return;
        if (!route({.action = "msg",
                    .level = static_cast<uint64_t>(lvl),
                    .text = std::string(s)}))
            fallback().log(lvl, s);
    }

    void logEI(const nix::ErrorInfo & ei) noexcept override
    {
        // RENDERED, the way JSONLogger renders it (logging.cc:283),
        // and the parts beside it in the record a failed call crosses
        // with, so the two error shapes are one (huggorm#103).
        if (ei.level > effective_verbosity())
            return;
        std::ostringstream rendered;
        nix::showErrorInfo(rendered, ei, nix::loggerSettings.showTrace.get());
        if (!route({.action = "msg",
                    .level = static_cast<uint64_t>(ei.level),
                    .text = rendered.str(),
                    .info = info_record<ErrorInfo>(ei)}))
            fallback().logEI(ei);
    }

    void startActivity(nix::ActivityId act, nix::Verbosity lvl,
                       nix::ActivityType type, const std::string & s,
                       LoggerFields fields, nix::ActivityId parent) noexcept override
    {
        if (!route({.action = "start",
                    .level = static_cast<uint64_t>(lvl),
                    .id = act,
                    .parent = parent,
                    .type = static_cast<uint64_t>(type),
                    .text = s,
                    .fields = convert(fields)}) && lvl <= effective_verbosity())
            fallback().startActivity(act, lvl, type, s, fields, parent);
    }

    void stopActivity(nix::ActivityId act) noexcept override
    {
        if (!route({.action = "stop", .id = act}))
            fallback().stopActivity(act);  // no level to gate on
    }

    void result(nix::ActivityId act, nix::ResultType type,
                LoggerFields fields) noexcept override
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
     * (logging.cc:42), and huggorm#14 wants this protocol to run
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
    // Templates, because a 2.35 `Field` carries a tag and a 2.36 one
    // is a variant, and only a template discards the other spelling.
    template <typename Field>
    static LogField field_of(const Field & f)
    {
        if constexpr (requires { f.type; }) {
            return f.type == Field::tInt ? LogField{.is_int = true, .integer = f.i}
                                         : LogField{.is_int = false, .text = f.s};
        } else {
            if (const auto * i = std::get_if<uint64_t>(&f))
                return LogField{.is_int = true, .integer = *i};
            return LogField{.is_int = false, .text = std::get<std::string>(f)};
        }
    }

    template <typename Fields>
    static std::vector<LogField> convert(const Fields & fields)
    {
        std::vector<LogField> out;
        out.reserve(fields.size());
        for (const auto & f : fields)
            out.push_back(field_of(f));
        return out;
    }

    /**
     * Where a record goes when no queue claimed it.
     *
     * The logger nix itself installs at static init
     * (`logging.cc:35`), built here a second time because
     * `install_log_tap` no longer keeps the first one. It writes
     * descriptor 2 and gates on `nix::verbosity`.
     *
     * IT IS NOT A SUFFICIENT GATE for a daemon's lines. The daemon
     * narrates at the level `setOptions` sent it, and the client
     * re-raises every line with `printError`
     * (worker-protocol-connection.cc:75). That ERASES the daemon's
     * level: the line arrives as lvlError, passes every gate here
     * and every gate above, and lands on stderr. `VerbosityDemand`
     * keeps that level at what a subscriber asked for (huggorm#95).
     *
     * NO SECOND CEILING. A `level <= lvlWarn` cut was written into
     * huggorm#89 first, on the argument that a library must not
     * narrate uninvited. The probe refuted it: at the default
     * verbosity nothing above `lvlWarn` reaches stderr anyway, so
     * the cut's only live effect is to silence a caller who asked
     * for more - a silent drop, and this repo's named failure mode.
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
/**
 * The pin for `nix::verbosity`: `HUGGORM_LOG_CEILING`, a level name
 * or a number, and CHATTY when it is unset or empty.
 *
 * CHATTY is nanopynix-bindings' measurement (`nix_util.cpp`): the
 * widest pin that costs evaluation, store queries and flake fetches
 * nothing. DEBUG formats every `debug()` site, and cost a flake
 * evaluation its RPC deadline there.
 *
 * A value that names no level REFUSES the import. Keeping the default
 * instead would ignore what the caller asked for with nothing said.
 */
inline nix::Verbosity log_ceiling()
{
    const char * raw = std::getenv("HUGGORM_LOG_CEILING");
    if (raw == nullptr || *raw == '\0')
        return nix::lvlChatty;
    std::string key(raw);
    for (auto & c : key)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    static constexpr std::array<std::string_view, std::to_underlying(nix::lvlVomit) + 1> names{
        "error", "warn", "notice", "info", "talkative", "chatty", "debug", "vomit"};
    for (std::size_t i = 0; i < names.size(); ++i)
        if (key == names[i] || key == std::to_string(i))
            return static_cast<nix::Verbosity>(i);
    throw std::invalid_argument(
        "HUGGORM_LOG_CEILING is '" + std::string(raw)
        + "', and it takes a level name (error to vomit) or 0 to 7");
}

/**
 * Put `tap` in Nix's logger slot, whatever the slot holds: a
 * `unique_ptr` to 2.34, a raw pointer from 2.35. The raw one owns
 * nothing, and the tap lives for the process either way. A template,
 * because `if constexpr` discards the other branch only there.
 */
template <typename Slot>
void set_logger(Slot & slot, std::unique_ptr<nix::Logger> tap)
{
    if constexpr (std::is_pointer_v<Slot>)
        slot = tap.release();
    else
        slot = std::move(tap);
}

inline void install_log_tap()
{
    // The one write to `nix::verbosity`. This runs at import, before
    // any Nix thread exists, so thread creation orders every later
    // read after it (`VerbosityDemand` says why a later write races).
    //
    // The daemon starts at lvlInfo, not at the pin: `setOptions`
    // would otherwise send the pin to every daemon, whose lines then
    // reach stderr as errors.
    nix::verbosity = log_ceiling();
    nix::remoteVerbosity.store(std::to_underlying(nix::lvlInfo), std::memory_order_relaxed);
    set_logger(nix::logger, std::make_unique<LogTap>());
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
    // The thread level keeps the record here rather than on every
    // other thread, and its demand tells a daemon opened from now on
    // to narrate at that level.
    //
    // `set` owns the pairing of the two demands, because doing it
    // here by hand is exactly what huggorm#96 got wrong.
    thread_level().set(static_cast<nix::Verbosity>(level));
    auto & slot = thread_queue();
    if (slot)
        slot->close();
    slot = std::make_shared<LogQueue>(capacity);
    return slot;
}

/** Stop recording. A queue already handed out still drains. */
inline void unsubscribe_logs()
{
    // Back to following the process default, rather than to a number.
    // A thread that stops subscribing has no opinion again, and
    // leaving its old level behind would filter records it no longer
    // reads - including the ones the process-wide sink wants.
    thread_level().clear();
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
 * question huggorm#32 opened. It is answered ABOVE this layer: the
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
    // RAISES BOTH, or this subscription's level is a lie. The records this sink exists for are raised on nix's own
    // threads - a fetcher, a file transfer, a build - and none of
    // them ever sets a level, so they read the default. Leaving it at
    // `lvlInfo` would have the tap drop a debug record before the
    // queue that asked for it ever saw one.
    //
    // Process-wide state changed by one subscriber, which is what
    // this function already is: it REPLACES any subscription that was
    // there. `unsubscribe_process_logs` puts the default back.
    set_default_verbosity(static_cast<nix::Verbosity>(level));
    auto fresh = std::make_shared<LogQueue>(capacity);
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
    // Back to nix's own default (`logging.cc:150`), so a thread with
    // no level of its own sees what it saw before anyone subscribed.
    //
    // The daemon level drops to what the per-thread subscriptions
    // still need. huggorm#95 measured what leaving it up cost: 1052
    // daemon debug lines on an unsubscribed caller's stderr.
    default_verbosity().store(std::to_underlying(nix::lvlInfo), std::memory_order_relaxed);
    set_process_demand(-1);
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
