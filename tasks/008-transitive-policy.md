# Transitive policy enforcement; returned_module adoption

Review finding 8. Pool-wrapper policy drops affine returns by DIRECT
return_type match only, and returned_module always emits bare
hop-returns - a pool wrapper returning a pool type whose methods
return affine values yields raw sync affine objects usable from any
thread (attach_runner's guard can never fire).

Fix: mirror adopt-or-drop logic in returned_module; enforce policies
transitively over the manifest graph at generation time.

## Update 2026-08-25

`hide` is gone (removed with the codegen hygiene pass); that half of
the finding is closed. The transitive half stands, unchanged and still
latent: no returned type currently returns another wrapper type, so
nothing exercises it.

The policy-drop check in smoke_test was VACUOUS until 2026-08-25 - it
compared a string against a list of dicts. It now compares names and
carries its control case. So the direct rule is genuinely pinned for
the first time; the transitive rule still has no test because it has
no case.

Adjacent invariant now enforced (wire-codec work): a wire-value must
be threading "pool". A value that may not leave its thread cannot be
serialized off it. That is the same family of reasoning; a general
policy-consistency pass over the manifest graph should absorb it.
