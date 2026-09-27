#pragma once

/**
 * A fetcher settings object for one call, outside any evaluator.
 *
 * Not `settings.hpp`. That header owns the process's registered copy,
 * and a function-local static in a second extension module is a second
 * copy that nothing registers. This one registers nothing: it reads
 * what `globalConfig` holds, and the `eval` module registers the fetcher
 * settings there at import (`_settings_init`), before any other module
 * of the package can run.
 */

#include <map>
#include <memory>
#include <string>

#include "nix/fetchers/fetch-settings.hh"
#include "nix/util/config-global.hh"
#include "nix/util/configuration.hh"
#include "nix/util/error.hh"

namespace huggorm {

/**
 * What the process has, then `own` over it.
 *
 * On the heap, because a `nix::Config` holds pointers to its own
 * members and cannot move. A name that is not a fetcher setting raises,
 * as `Evaluator` refuses one.
 */
inline std::unique_ptr<nix::fetchers::Settings> fetch_settings(const std::map<std::string, std::string> & own)
{
    auto settings = std::make_unique<nix::fetchers::Settings>();
    std::map<std::string, nix::Config::SettingInfo> overridden;
    nix::globalConfig.getSettings(overridden, /*overriddenOnly=*/true);
    for (auto & [name, info] : overridden)
        settings->set(name, info.value);
    for (auto & [name, value] : own)
        if (!settings->set(name, value))
            throw nix::UsageError("'%s' is not a fetcher setting", name);
    return settings;
}

}  // namespace huggorm
