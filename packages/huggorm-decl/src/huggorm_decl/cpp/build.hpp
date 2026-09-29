#pragma once
// `nix::Builder`, where the Nix has one.
//
// Nix 2.36 moved `buildPaths`, `buildPathsWithResults` and `ensurePath`
// off `nix::Store` and onto a `Builder` that `Store::getBuilder` hands
// out, declared in a header 2.34 and 2.35 do not have. A declaration's
// `@needs` names one header for every Nix, so it names this one; the
// body's `NIX_VERSION` arm decides which call is made (huggorm#55).

#if __has_include("nix/store/build.hh")
#  include "nix/store/build.hh"
#endif
