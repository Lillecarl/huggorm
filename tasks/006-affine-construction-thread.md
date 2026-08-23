# Affine construction on wrong thread; kwargs unwrap gap

Review finding 6 (proven). runtime.unwrap_arg calls ensure()
synchronously on the caller's worker, constructing affine objects off
their home thread (violates the documented invariant). Emitted lazy
factories unwrap *args but forward **kwargs raw, so a wrapper passed
as kwarg hands the async shell to the sync constructor.

Fix: route ensure() through the owner's executor, or require eager
construction before a wrapper is argument-passable; unwrap kwargs too.
