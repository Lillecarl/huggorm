#pragma once

/**
 * A settings object for one call, outside any evaluator.
 *
 * Not `settings.hpp`. That header owns the process's registered copies,
 * and a function-local static in a second extension module is a second
 * copy that nothing registers. This one registers nothing: it reads
 * what `globalConfig` holds, and the `eval` module registers the
 * evaluator, fetcher and flake settings there at import
 * (`_settings_init`), before any other module of the package can run.
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
 * On the heap, because a `nix::Config` holds pointers to its own
 * members and cannot move. A name that `S` does not hold raises, as
 * `Evaluator` refuses one: a setting that changes nothing must say so.
 */
template<typename S>
std::unique_ptr<S> call_settings(const std::map<std::string, std::string> & own)
{
    auto settings = std::make_unique<S>();
    std::map<std::string, nix::Config::SettingInfo> overridden;
    nix::globalConfig.getSettings(overridden, /*overriddenOnly=*/true);
    for (auto & [name, info] : overridden)
        settings->set(name, info.value);
    for (auto & [name, value] : own)
        if (!settings->set(name, value))
            throw nix::UsageError("'%s' is not a setting this call takes", name);
    return settings;
}

}  // namespace huggorm
