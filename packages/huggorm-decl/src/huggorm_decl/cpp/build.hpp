#pragma once
// Building, whatever the lane.
//
// Nix 2.36 moved `buildPaths`, `buildPathsWithResults` and `ensurePath`
// off `nix::Store` and onto a `Builder` that `Store::getBuilder` hands
// out, declared in a header 2.34 and 2.35 do not have. Each helper
// tests for `getBuilder` and makes the call the lane has. Templates,
// because only a dependent expression lets `if constexpr` discard the
// spelling a lane lacks.

#if __has_include("nix/store/build.hh")
#  include "nix/store/build.hh"
#endif
#include "nix/store/store-api.hh"

namespace huggorm {

/** `buildPaths`, with `eval` as the store that holds the `.drv` files. */
template <typename S, typename Targets, typename E>
void build_paths(S & store, const Targets & targets, nix::BuildMode mode, E eval)
{
    if constexpr (requires { store.getBuilder(eval); })
        store.getBuilder(eval)->buildPaths(targets, mode);
    else
        store.buildPaths(targets, mode, eval);
}

/** `buildPathsWithResults`, with `eval` as `build_paths` takes it. */
template <typename S, typename Targets, typename E>
auto build_paths_with_results(S & store, const Targets & targets, nix::BuildMode mode, E eval)
{
    if constexpr (requires { store.getBuilder(eval); })
        return store.getBuilder(eval)->buildPathsWithResults(targets, mode);
    else
        return store.buildPathsWithResults(targets, mode, eval);
}

/** `ensurePath`: substitute `path` if it is not valid. */
template <typename S, typename P>
void ensure_path(S & store, const P & path)
{
    if constexpr (requires { store.getBuilder(); })
        store.getBuilder()->ensurePath(path);
    else
        store.ensurePath(path);
}

}  // namespace huggorm
