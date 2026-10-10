#pragma once
// Conversions the emitted units call, included by every unit.
//
// Inline templates and inline functions, so a unit that calls none of
// them pays nothing. Nothing here names a Nix type: a helper that does
// belongs in `huggorm_decl/cpp/`, where a Nix version can branch.

#include <map>
#include <string>
#include <string_view>
#include <vector>

#include <nanobind/nanobind.h>

namespace nb = nanobind;

// libstore answers with std::set and takes std::set; the binding hands
// Python a list, because these answers are sorted and a Python set
// would throw that away. Neither direction copies a string or parses
// a name.

/** A libstore set as the list a caller reads. Sorted, because the set is. */
template <typename T>
inline std::vector<typename T::value_type> as_list(const T & items)
{
    return {items.begin(), items.end()};
}

/**
 * A libstore map as the `std::map<std::string, V>` a caller reads.
 * libstore keys many maps with a transparent `std::less<>`, such as
 * `StringPairs`, and nanobind casts only the plain comparator.
 */
template <typename T>
inline std::map<std::string, typename T::mapped_type> as_map(const T & items)
{
    return {items.begin(), items.end()};
}

/** The mirror: a list as the set libstore takes. */
template <typename T, typename I>
inline T as_set(const I & items)
{
    return {items.begin(), items.end()};
}

/** A set of printable values as the strings a caller reads. */
template <typename T>
inline std::vector<std::string> to_strings(const T & items)
{
    std::vector<std::string> out;
    out.reserve(items.size());
    for (auto & item : items)
        out.push_back(item.to_string());
    return out;
}

// `nb::bytes` has only explicit constructors, so neither direction
// happens on its own. `to_bytes`, not `as_bytes`: <span> declares a
// `std::as_bytes` that overload resolution finds first.

/** A string as the bytes a caller reads. */
inline nb::bytes to_bytes(std::string_view s)
{
    return nb::bytes(s.data(), s.size());
}

inline std::vector<nb::bytes> to_bytes(const std::vector<std::string> & items)
{
    std::vector<nb::bytes> out;
    out.reserve(items.size());
    for (auto & s : items)
        out.push_back(to_bytes(s));
    return out;
}

/** The mirror: bytes back into the string a value holds, unread. */
inline std::string from_bytes(const nb::bytes & b)
{
    return std::string(b.c_str(), b.size());
}

inline std::vector<std::string> from_bytes(const std::vector<nb::bytes> & items)
{
    std::vector<std::string> out;
    out.reserve(items.size());
    for (auto & b : items)
        out.push_back(from_bytes(b));
    return out;
}

namespace huggorm {

// A function rather than a cast at each call site, because
// `nb::tuple(h.attr("x"))` does not resolve: `attr` hands back an
// accessor, and the constructor that CONVERTS takes a handle.
//
// A dict's items are SORTED, because two equal dicts can iterate in
// different orders and must hash equal. Its keys are strings, so they
// sort and never tie.

/** A list as a tuple and a dict as its sorted items, at any depth. */
inline nb::object hashable(nb::handle h)
{
    if (nb::isinstance<nb::list>(h)) {
        nb::list out;
        for (nb::handle item : h)
            out.append(hashable(item));
        return nb::tuple(out);
    }
    if (nb::isinstance<nb::dict>(h)) {
        nb::list out;
        for (auto [key, value] : nb::borrow<nb::dict>(h))
            out.append(nb::make_tuple(key, hashable(value)));
        out.attr("sort")();
        return nb::tuple(out);
    }
    return nb::borrow(h);
}

}  // namespace huggorm
