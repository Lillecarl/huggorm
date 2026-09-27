#pragma once

/**
 * A settings object for one call, outside any evaluator.
 *
 * It registers nothing: it reads what `globalConfig` holds, where
 * libcmd registers the evaluator, fetcher and flake settings when it
 * loads (`settings.hpp`).
 */

#include <map>
#include <memory>
#include <string>

#include "nix/util/config-global.hh"
#include "nix/util/configuration.hh"
#include "nix/util/error.hh"

namespace huggorm {

/**
 * What the process has, then `own` over it.
 *
 * By value, as `replay_configured` in `settings.hpp` copies, and for
 * its reason: `reset_overridden` clears the mark and keeps the value.
 *
 * On the heap, because a `nix::Config` holds pointers to its own
 * members and cannot move. A name that `S` does not hold raises, as
 * `Evaluator` refuses one: a setting that changes nothing must say so.
 */
template<typename S>
std::unique_ptr<S> call_settings(const std::map<std::string, std::string> & own)
{
    auto settings = std::make_unique<S>();
    std::map<std::string, nix::Config::SettingInfo> process, defaults;
    nix::globalConfig.getSettings(process);
    settings->getSettings(defaults);
    for (auto & [name, info] : defaults)
        if (auto set = process.find(name); set != process.end() && set->second.value != info.value)
            settings->set(name, set->second.value);
    for (auto & [name, value] : own)
        if (!settings->set(name, value))
            throw nix::UsageError("'%s' is not a setting this call takes", name);
    return settings;
}

}  // namespace huggorm
