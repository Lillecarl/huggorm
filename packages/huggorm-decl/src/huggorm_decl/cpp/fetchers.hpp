#pragma once

/**
 * One call shape for building a `nix::fetchers::Input`, whatever the
 * lane: 2.35 takes the fetcher settings first, 2.36 does not. The same
 * test as `flakeref.hpp`, for the same reason.
 */

#include <utility>

#include "nix/fetchers/fetch-settings.hh"
#include "nix/fetchers/fetchers.hh"

namespace huggorm {

/** `Input::fromURL(url)`, with `fetch` where the lane takes it. */
template <typename Url>
nix::fetchers::Input input_of(const nix::fetchers::Settings & fetch, const Url & url)
{
    if constexpr (requires { nix::fetchers::Input::fromURL(fetch, url); })
        return nix::fetchers::Input::fromURL(fetch, url);
    else
        return nix::fetchers::Input::fromURL(url);
}

/** `Input::fromAttrs(attrs)`, with `fetch` where the lane takes it. */
template <typename Attrs>
nix::fetchers::Input input_of_attrs(const nix::fetchers::Settings & fetch, Attrs && attrs)
{
    if constexpr (requires { nix::fetchers::Input::fromAttrs(fetch, std::forward<Attrs>(attrs)); })
        return nix::fetchers::Input::fromAttrs(fetch, std::forward<Attrs>(attrs));
    else
        return nix::fetchers::Input::fromAttrs(std::forward<Attrs>(attrs));
}

}  // namespace huggorm
