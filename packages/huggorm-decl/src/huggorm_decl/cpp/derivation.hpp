#pragma once

/**
 * A derivation's inputs as two sets, whatever the lane.
 *
 * 2.35 keeps `inputSrcs` and `inputDrvs`. 2.36 keeps one `inputs` set,
 * and `FullInputs::fromSet` is upstream's own split back into the two.
 * `FullInputs` is a type in a namespace 2.35 lacks, and a type name
 * cannot wait for instantiation the way a call can, so the test is
 * for the header that declares it.
 */

#include "nix/store/derivations.hh"

#if __has_include("nix/store/derivation/full-inputs.hh")
#  include "nix/store/derivation/full-inputs.hh"
#  define HUGGORM_FULL_INPUTS 1
#endif

namespace huggorm {

/** Store paths the build reads that no derivation makes. */
inline nix::StorePathSet input_srcs(const nix::Derivation & drv)
{
#ifdef HUGGORM_FULL_INPUTS
    return nix::derivation::FullInputs::fromSet(drv.inputs).srcs;
#else
    return drv.inputSrcs;
#endif
}

/** The derivations the build needs, and which of their outputs. */
inline auto input_drvs(const nix::Derivation & drv)
{
#ifdef HUGGORM_FULL_INPUTS
    return nix::derivation::FullInputs::fromSet(drv.inputs).drvs;
#else
    return drv.inputDrvs;
#endif
}

}  // namespace huggorm
