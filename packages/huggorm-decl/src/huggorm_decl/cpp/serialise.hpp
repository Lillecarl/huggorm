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

#include <algorithm>
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

    /**
     * Up to `n` bytes, and none at the end of the stream. One chunk at
     * most, so `read(nar_size)` does not allocate the whole NAR.
     */
    virtual std::string read(std::uint64_t n)
    {
        std::string out(std::min<std::uint64_t>(n, 64 * 1024), '\0');
        try {
            out.resize(below("Source").read(out.data(), out.size()));
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

/**
 * `below` behind a 32 KiB buffer, for a call that hands it to Nix. Nix
 * writes a NAR in pieces of a few bytes, and each piece is a call into
 * Python: 6515 for a 45 KB NAR of 100 files (huggorm#155). The call that
 * makes one flushes it, because a destructor cannot report a failed write.
 */
class SinkBuffer : public nix::BufferedSink
{
public:
    explicit SinkBuffer(nix::Sink & below)
        : below_(below)
    {
    }

protected:
    void writeUnbuffered(std::string_view data) override
    {
        below_(data);
    }

private:
    nix::Sink & below_;
};

/**
 * The next `size` bytes of `below`, read in pieces of up to 32 KiB. It
 * never asks for more than `size`: what follows a NAR belongs to the
 * next reader, and a stream that holds nothing more would block.
 */
class SourceBuffer : public nix::BufferedSource
{
public:
    SourceBuffer(nix::Source & below, std::uint64_t size)
        : sized_(below, size)
    {
    }

protected:
    size_t readUnbuffered(char * data, size_t len) override
    {
        return sized_.read(data, len);
    }

private:
    nix::SizedSource sized_;
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
