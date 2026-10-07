#pragma once
/**
 * Byte streams Python reads and writes: `nix::Source` and `nix::Sink`,
 * each with a hook a Python subclass overrides (huggorm#149).
 *
 * One class serves both directions. A Python subclass is a stream Nix
 * reads or writes, as `Store.nar_from_path` takes. A wrapper over a
 * stream Nix made is what a Python store's override gets: its hook
 * forwards to that stream.
 *
 * A wrapper is valid only during the call it was made for. Copies share
 * one `Target`, and `Ended` cuts it when the call returns, so a stream
 * Python keeps after that raises instead of reaching a dead one.
 */

#include <cstring>
#include <memory>
#include <string>

#include "nix/util/error.hh"
#include "nix/util/serialise.hh"

namespace huggorm {

template<typename Stream>
struct Target
{
    Stream * stream = nullptr;
    bool ended = false;
};

template<typename Stream>
class Below
{
public:
    Below()
        : target_(std::make_shared<Target<Stream>>())
    {
    }

    explicit Below(Stream & stream)
        : target_(std::make_shared<Target<Stream>>(Target<Stream>{&stream}))
    {
    }

    /** Cut every copy off the stream below. */
    void end()
    {
        target_->stream = nullptr;
        target_->ended = true;
    }

protected:
    Stream & below(const char * what) const
    {
        if (target_->ended)
            throw nix::Error("this %s belongs to a call that has returned", what);
        if (!target_->stream)
            throw nix::Error("this %s has nothing below it: a subclass overrides it", what);
        return *target_->stream;
    }

private:
    std::shared_ptr<Target<Stream>> target_;
};

class Sink : public nix::Sink, public Below<nix::Sink>
{
public:
    using Below::Below;

    virtual ~Sink() = default;

    virtual void write(const std::string & data)
    {
        below("Sink")(data);
    }

    void operator()(std::string_view data) override
    {
        write(std::string(data));
    }
};

class Source : public nix::Source, public Below<nix::Source>
{
public:
    using Below::Below;

    virtual ~Source() = default;

    /** Up to `n` bytes, and none at the end of the stream. */
    virtual std::string read(std::uint64_t n)
    {
        std::string out(n, '\0');
        try {
            out.resize(below("Source").read(out.data(), n));
        } catch (nix::EndOfFile &) {
            out.clear();
        }
        return out;
    }

    size_t read(char * data, size_t len) override
    {
        auto got = read(static_cast<std::uint64_t>(len));
        if (got.empty())
            throw nix::EndOfFile("the Source ended");
        if (got.size() > len)
            throw nix::Error("a Source answered %d bytes when asked for at most %d", got.size(), len);
        std::memcpy(data, got.data(), got.size());
        return got.size();
    }
};

/** Ends `stream` when the call it was made for returns or throws. */
template<typename Stream>
class Ended
{
public:
    explicit Ended(Stream & stream)
        : stream_(stream)
    {
    }

    Ended(const Ended &) = delete;
    Ended & operator=(const Ended &) = delete;

    ~Ended()
    {
        stream_.end();
    }

private:
    Stream & stream_;
};

} // namespace huggorm
