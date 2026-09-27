#pragma once
// Two evaluation errors Nix reports only from inside the evaluator:
// `ExprSelect::eval` for a missing attribute (eval.cc:1438) and
// `prim_elemAt` for an index past the end. `Value.get` and `Value.at`
// are not those code paths, so they throw these instead.
//
// Distinct types so the emitted translator can tell them from a plain
// `nix::EvalError` and a caller can catch one kind. `decl/errors.py`
// declares each; the translator catches them before `EvalError`
// because they derive from it.

#include "nix/expr/eval-error.hh"
#include "nix/util/suggestions.hh"

#include <utility>

namespace huggorm {

struct MissingAttribute : nix::EvalError
{
    using nix::EvalError::EvalError;

    // Not `EvalState::error<T>`: libexpr instantiates `EvalErrorBuilder`
    // for its own classes only, so this one would not link. `err` is
    // protected, so a member is the one place that can attach them.
    void with_suggestions(nix::Suggestions suggestions)
    {
        err.suggestions = std::move(suggestions);
    }
};

struct ListIndex : nix::EvalError
{
    using nix::EvalError::EvalError;
};

} // namespace huggorm
