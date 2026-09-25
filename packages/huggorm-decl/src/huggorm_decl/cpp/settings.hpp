#pragma once

/**
 * The settings objects nix.conf fills in for an evaluator.
 *
 * `nix::settings` registers itself on `globalConfig` from libstore, so
 * the store reads nix.conf with no help. `EvalSettings` and
 * `fetchers::Settings` do not: `nix` registers them from libcmd
 * (`common-eval-args.cc`), which this repo does not link. With no
 * registration, `loadConfFile` puts `pure-eval`, `nix-path`,
 * `restrict-eval` and every fetcher setting in
 * `globalConfig.unknownSettings`, and nothing reads them again. Nothing
 * warns either: `warnUnknownSettings` is called from libmain.
 *
 * So these two are registered here, the way libcmd does it, and each
 * `Evaluator` copies what the file set onto its own pair (tasks/097).
 *
 * A registration after `loadConfFile` sees nothing. `Config::addSetting`
 * consults the config's OWN unknown map, and the values sit in
 * `globalConfig`'s. `reapplyUnknownSettings` sends them through
 * `set` again, which is what `plugin.cc` does after a plugin registers.
 * That makes the order of the store module's `initLibStore` and this
 * startup irrelevant.
 *
 * Function-local statics, and only `eval` includes this header. A
 * second module including it would register a second copy in its own
 * shared object.
 */

#include <map>
#include <string>

#include "nix/expr/eval-settings.hh"
#include "nix/expr/eval.hh"
#include "nix/fetchers/fetch-settings.hh"
#include "nix/store/globals.hh"
#include "nix/util/config-global.hh"
#include "nix/util/configuration.hh"

namespace huggorm {

/** What nix.conf and NIX_CONFIG say, and never handed to a state. */
struct ConfiguredSettings
{
    nix::fetchers::Settings fetch;
    nix::EvalSettings eval{nix::settings.readOnlyMode};
};

inline ConfiguredSettings & configured_settings()
{
    static ConfiguredSettings configured;
    return configured;
}

inline void register_configured_settings()
{
    static bool done = [] {
        auto & configured = configured_settings();
        static const nix::GlobalConfig::Register fetch(&configured.fetch);
        static const nix::GlobalConfig::Register eval(&configured.eval);
        nix::globalConfig.reapplyUnknownSettings();
        return true;
    }();
    (void) done;
}

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

/**
 * Copy the configured pair onto a state's own, before the state exists.
 *
 * Before, because `EvalState`'s constructor reads them: `pure-eval`
 * decides whether `builtins.currentTime` is created at all. Returns a
 * value so an `Evaluator` member initialiser can call it in order.
 */
inline bool apply_configured(nix::fetchers::Settings & fetch, nix::EvalSettings & eval)
{
    register_configured_settings();
    auto & configured = configured_settings();
    replay_overridden(fetch, configured.fetch);
    replay_overridden(eval, configured.eval);
    return true;
}

}  // namespace huggorm
