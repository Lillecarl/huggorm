#pragma once

/**
 * The settings objects nix.conf fills in for an evaluator.
 *
 * libcmd's own: `nix::evalSettings`, `nix::fetchSettings` and
 * `nix::flakeSettings`, which `common-eval-args.cc` registers on
 * `globalConfig` when the library loads, as the `nix` CLI has them.
 * Each `Evaluator` copies what the file set onto its own eval and
 * fetcher pair (tasks/097).
 *
 * NOT a second registered copy. `GlobalConfig::set` stops at the first
 * registered object that takes a name, so a copy registered after
 * libcmd's never sees a value.
 */

#include <map>
#include <string>

// `GlobalConfig::toJSON` returns one, and config-global.hh only
// forward-declares it.
#include <nlohmann/json.hpp>

#include "nix/cmd/common-eval-args.hh"
#include "nix/expr/eval-settings.hh"
#include "nix/expr/eval.hh"
#include "nix/fetchers/fetch-settings.hh"
#include "nix/flake/settings.hh"
#include "nix/store/globals.hh"
#include "nix/util/config-global.hh"
#include "nix/util/configuration.hh"

namespace huggorm {

/**
 * Set on `target` every value the file set on `source`.
 *
 * Overridden only, so a state keeps the compiled default wherever the
 * file is silent. `set` answers false for a setting Nix refused, such
 * as an experimental one whose feature is off, and Nix has warned
 * already.
 */
inline void replay_overridden(nix::Config & target, const nix::Config & source)
{
    std::map<std::string, nix::Config::SettingInfo> overridden;
    source.getSettings(overridden, /*overriddenOnly=*/true);
    for (auto & [name, info] : overridden)
        target.set(name, info.value);
}

/** One state's own settings: a name, and its value as nix.conf spells it. */
using Settings = std::map<std::string, std::string>;

/**
 * Copy the configured pair onto a state's own, then the state's own
 * settings over them, before the state exists.
 *
 * Before, because `EvalState`'s constructor reads them: `pure-eval`
 * decides whether `builtins.currentTime` is created at all. Returns a
 * value so an `Evaluator` member initialiser can call it in order.
 *
 * One map for both objects, because no name is in both. A name
 * neither holds raises: a store setting here would change nothing,
 * and saying nothing would hide that.
 */
inline bool apply_configured(nix::fetchers::Settings & fetch, nix::EvalSettings & eval, const Settings & own)
{
    replay_overridden(fetch, nix::fetchSettings);
    replay_overridden(eval, nix::evalSettings);
    for (auto & [name, value] : own)
        if (!eval.set(name, value) && !fetch.set(name, value))
            throw nix::UsageError("'%s' is not an evaluator or fetcher setting", name);
    return true;
}

}  // namespace huggorm
