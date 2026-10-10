#pragma once

/**
 * Store calls whose shape differs between lanes.
 *
 * `parseJsonAndValidate` is a static of `nix::Derivation` in 2.35 and a
 * free function in `nix::derivation` in 2.36. 2.35 has no such
 * namespace, and a qualified name into a namespace that does not exist
 * is an error even in a discarded branch. So the namespace is opened
 * here, empty, and the call is unqualified behind a using-directive:
 * its arguments are dependent, so lookup waits for the arm that runs.
 */

#include "nix/store/derivations.hh"

namespace nix::derivation {}

namespace huggorm {

/** A derivation from Nix's JSON, its deferred output paths filled in. */
template <typename D = nix::Derivation, typename S, typename J>
auto derivation_from_json(S & store, const J & json)
{
    if constexpr (requires { D::parseJsonAndValidate(store, json); })
        return D::parseJsonAndValidate(store, json);
    else {
        using namespace nix::derivation;
        return parseJsonAndValidate(store, json);
    }
}

}  // namespace huggorm
