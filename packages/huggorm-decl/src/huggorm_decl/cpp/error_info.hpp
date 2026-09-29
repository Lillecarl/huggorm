#pragma once
// A `nix::ErrorInfo`, read into the records `decl/path.py` declares.
//
// Templates over the record types, so the catch chain names the
// record each part reads into, and a part that is not a record fails
// to compile. The designated initialisers name each field, so a record
// that drops or reorders a field fails to compile here too.

#include <algorithm>
#include <cstddef>
#include <cstdint>
#include <iterator>
#include <memory>
#include <optional>
#include <sstream>
#include <string>
#include <vector>

#include "nix/util/error.hh"
#include "nix/util/position.hh"

namespace huggorm {

// An error crosses the wire in the `grpc-status-details-bin` header,
// and a deep trace runs to tens of kilobytes. Past a peer's header
// limit, that is a protocol error and not a shorter message.
inline constexpr std::size_t max_traces = 32;
inline constexpr std::size_t max_hint_bytes = 4096;

inline std::string clamp(std::string text)
{
    if (text.size() <= max_hint_bytes)
        return text;
    text.resize(max_hint_bytes);
    return text + "... [truncated]";
}

/**
 * The file a position is in: the physical path where it has one, so
 * an editor can open it.
 *
 * Otherwise Nix's own name for the origin, `«string»`, `«stdin»` or
 * `«none»`. `Pos::print` is the one place that names them, and it
 * writes the name before the first ':', which none of them holds.
 */
inline std::string position_file(const nix::Pos & pos)
{
    if (auto source = pos.getSourcePath()) {
        if (auto physical = source->getPhysicalPath())
            return physical->string();
        return source->to_string();
    }
    std::ostringstream out;
    pos.print(out, true);
    auto text = out.str();
    return text.substr(0, text.find(':'));
}

template <typename Position>
std::optional<Position> position_of(const std::shared_ptr<const nix::Pos> & pos)
{
    if (!pos || !*pos)
        return std::nullopt;
    return Position{
        .file = position_file(*pos),
        .line = static_cast<std::int64_t>(pos->line),
        .column = static_cast<std::int64_t>(pos->column),
    };
}

/**
 * The part a `NixError` carries beyond its message.
 *
 * `decl/errors.py` names this as the reader of that part, and the
 * emitted catch chain calls it with the part's record type.
 */
// Not an overload of `error_info`: the catch chain passes
// `error_info<Info>` as a function, and an overload set deduces no type.
template <typename Info>
Info info_record(const nix::ErrorInfo & info)
{
    using Position = typename decltype(Info::pos)::value_type;
    using Trace = typename decltype(Info::traces)::value_type;

    // Nix pushes each frame to the front, so the list is outermost
    // first. The frames kept are the last ones, nearest the error.
    auto dropped = info.traces.size() - std::min(info.traces.size(), max_traces);
    std::vector<Trace> traces;
    for (auto it = std::next(info.traces.begin(), static_cast<std::ptrdiff_t>(dropped));
         it != info.traces.end(); ++it)
        traces.push_back(Trace{
            .hint = clamp(it->hint.str()),
            .pos = position_of<Position>(it->pos),
        });

    std::vector<std::string> suggestions;
    for (const auto & suggestion : info.suggestions.suggestions)
        suggestions.push_back(suggestion.suggestion);

    return Info{
        .level = static_cast<std::int64_t>(info.level),
        .msg = clamp(info.msg.str()),
        .pos = position_of<Position>(info.pos),
        .is_from_expr = info.isFromExpr,
        .status = static_cast<std::int64_t>(info.status),
        .traces = std::move(traces),
        .truncated = dropped > 0,
        .suggestions = std::move(suggestions),
    };
}

template <typename Info>
Info error_info(const nix::BaseError & e)
{
    return info_record<Info>(e.info());
}

}  // namespace huggorm
