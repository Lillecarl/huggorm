# Evaluation server: persistent state, watched files, background eval

Carl's vision (2026-08-23). The remote layer stops being a method
façade and becomes a long-lived evaluation service:

- One EvalState serves many connections over days. Clients create,
  Detach (see 002), exit; later clients Claim the same state. No
  reevaluation of unchanged input.
- Repeated evals run against warm caches on that state - fast AND
  correct Nix-direnv-style work.
- Server watches every non-Store file an evaluation touched
  (inotify). Watched expressions can be registered for background
  eager evaluation; next user-triggered eval is progressing or already
  done instead of starting cold.

Depends on 002 (escrowed handles outliving creators). Real value
needs the real libexpr (015) - the mock has no true evaluator or
file dependency graph to watch. Design the lifecycle contract here;
build after 015 lands.
