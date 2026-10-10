#pragma once

/**
 * One call shape for building a `nix::FlakeRef`, whatever the lane.
 *
 * 2.35 takes the fetcher settings first; 2.36 parses without them.
 * The test is the call, not a version name, so a lane between the two
 * gets whichever it compiles. Templates, because only a dependent
 * expression lets `if constexpr` discard the spelling a lane lacks.
 */

#include <filesystem>

#include "nix/fetchers/fetch-settings.hh"
#include "nix/flake/flakeref.hh"

namespace huggorm {

/** `nix::parseFlakeRef(url, dir)`, with `fetch` where the lane takes it. */
template <typename Url>
nix::FlakeRef
flake_ref_of(const nix::fetchers::Settings & fetch, const Url & url, const std::filesystem::path & dir)
{
    if constexpr (requires { nix::parseFlakeRef(fetch, url, dir); })
        return nix::parseFlakeRef(fetch, url, dir);
    else
        return nix::parseFlakeRef(url, dir);
}

/** `nix::FlakeRef::fromAttrs(attrs)`, with `fetch` where the lane takes it. */
template <typename Attrs>
nix::FlakeRef flake_ref_from_attrs(const nix::fetchers::Settings & fetch, const Attrs & attrs)
{
    if constexpr (requires { nix::FlakeRef::fromAttrs(fetch, attrs); })
        return nix::FlakeRef::fromAttrs(fetch, attrs);
    else
        return nix::FlakeRef::fromAttrs(attrs);
}

/**
 * Drops what the accessor under `path` cached of it. 2.34 drops the
 * path's entry; 2.35 has only the accessor's whole cache to drop.
 */
template <typename Path>
void invalidate_cache(const Path & path)
{
    if constexpr (requires { path.invalidateCache(); })
        path.invalidateCache();
    else
        path.accessor->invalidateCache();
}

}  // namespace huggorm
