#pragma once
// The Nix calls the emitter writes into every unit, by name.
//
// The emitter is generic and writes one text for every Nix version. A
// Nix API it reaches through a name here can change in one place, and
// a version branch (`__has_include`, `requires`) goes here, not there.

#include <string>

#include "nix/util/error.hh"

namespace huggorm {

/**
 * Refuses a word a vocabulary does not hold, as upstream's own parsers
 * refuse one: `parseHashAlgo` throws `UsageError` listing the words.
 */
template <typename... Args>
[[noreturn]] inline void refuse_word(const std::string & fs, const Args &... args)
{
    throw nix::UsageError(fs, args...);
}

/** An enumerator no word names: unreachable once a switch is exhaustive. */
[[noreturn]] inline void unknown_enumerator(const std::string & type)
{
    throw nix::Error("unknown " + type);
}

}  // namespace huggorm
