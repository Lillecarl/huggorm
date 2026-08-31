#pragma once
///@file
/// One decision, in both directions: which arms a derived path has on
/// the Python side.
///
/// Upstream's variants carry a `DerivedPathOpaque` in the opaque arm,
/// and that struct holds exactly one member - a `nix::StorePath`. On
/// the Python surface the wrapper buys nothing: a caller asking to
/// build a path already holds a StorePath, and a class that exists to
/// hold one and nothing else is a name to learn for no answer.
///
/// So the declared arms are `StorePath | ...Built`, which is a
/// DECISION about the surface rather than a binding of anything - and
/// a decision is what `cpp/` is for. It is written once here instead
/// of once per body that takes or returns one.
///
/// nanobind casts a `std::variant` natively, so nothing below is
/// about crossing the boundary. These only bridge the two spellings.
///
/// WHEN THIS MOVES INTO THE EMITTER: the second union whose C++ arm
/// wraps a declared arm in a one-member struct. One user is a helper;
/// two is a pattern, and the declaration already knows the arms - so
/// the emitter should write the visit from a declared fact rather
/// than a second header saying the same thing by hand.

#include "nix/store/derived-path.hh"

#include <variant>

namespace huggorm {

/** The arms of a SingleDerivedPath, as Python has them. */
using SingleArms = std::variant<nix::StorePath, nix::SingleDerivedPathBuilt>;

/** The arms of a DerivedPath, as Python has them. */
using DerivedArms = std::variant<nix::StorePath, nix::DerivedPathBuilt>;

inline SingleArms as_arms(const nix::SingleDerivedPath & p)
{
    if (auto * opaque = std::get_if<nix::SingleDerivedPath::Opaque>(&p.raw()))
        return opaque->path;
    return std::get<nix::SingleDerivedPath::Built>(p.raw());
}

inline DerivedArms as_arms(const nix::DerivedPath & p)
{
    if (auto * opaque = std::get_if<nix::DerivedPath::Opaque>(&p.raw()))
        return opaque->path;
    return std::get<nix::DerivedPath::Built>(p.raw());
}

inline nix::SingleDerivedPath from_arms(const SingleArms & a)
{
    if (auto * path = std::get_if<nix::StorePath>(&a))
        return nix::SingleDerivedPath::Opaque{*path};
    return std::get<nix::SingleDerivedPathBuilt>(a);
}

inline nix::DerivedPath from_arms(const DerivedArms & a)
{
    if (auto * path = std::get_if<nix::StorePath>(&a))
        return nix::DerivedPath::Opaque{*path};
    return std::get<nix::DerivedPathBuilt>(a);
}

/**
 * The `ref` upstream stores a nested derived path through.
 *
 * `SingleDerivedPathBuilt::drvPath` is a `ref<const
 * SingleDerivedPath>` - non-nullable by construction - so building one
 * from parts allocates. Not a decision, but the spelling is noisy
 * enough that a body saying it three times would be worse than a name.
 */
inline nix::ref<const nix::SingleDerivedPath> held(const SingleArms & a)
{
    return nix::make_ref<nix::SingleDerivedPath>(from_arms(a));
}

} // namespace huggorm
