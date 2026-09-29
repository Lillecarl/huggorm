#pragma once

/**
 * The settings objects nix.conf fills in for an evaluator.
 *
 * libcmd's own: `nix::evalSettings`, `nix::fetchSettings` and
 * `nix::flakeSettings`, which `common-eval-args.cc` registers on
 * `globalConfig` when the library loads, as the `nix` CLI has them.
 * Each `Evaluator` copies what the file set onto its own eval and
 * fetcher pair (huggorm#97).
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
 * Set on `target` every value of `source` that differs from
 * `target`'s own.
 *
 * By value, not by the `overridden` mark: `reset_overridden` clears
 * the mark and keeps the value, and nanopynix calls it right after
 * loading nix.conf. A value equal to the default is not set, so an
 * experimental setting whose feature is off does not warn. `set`
 * answers false for a setting Nix refused, and Nix has warned already.
 */
inline void replay_configured(nix::Config & target, const nix::Config & source)
{
    std::map<std::string, nix::Config::SettingInfo> configured, own;
    source.getSettings(configured);
    target.getSettings(own);
    for (auto & [name, info] : configured)
        if (auto mine = own.find(name); mine == own.end() || mine->second.value != info.value)
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
    replay_configured(fetch, nix::fetchSettings);
    replay_configured(eval, nix::evalSettings);
    // `builtins.getFlake`, `parseFlakeRef` and `flakeRefToString`, as
    // `nix`'s main.cc adds them. The `flakes` feature still gates each.
    nix::flakeSettings.configureEvalSettings(eval);
    for (auto & [name, value] : own)
        if (!eval.set(name, value) && !fetch.set(name, value))
            throw nix::UsageError("'%s' is not an evaluator or fetcher setting", name);
    return true;
}

}  // namespace huggorm
