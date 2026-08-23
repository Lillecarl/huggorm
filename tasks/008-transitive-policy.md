# Transitive policy enforcement; returned_module adoption

Review finding 8. Pool-wrapper policy drops affine returns by DIRECT
return_type match only, and returned_module always emits bare
hop-returns - a pool wrapper returning a pool type whose methods
return affine values yields raw sync affine objects usable from any
thread (attach_runner's guard can never fire). Also: extract_wrapper's
`hide` parameter is dead.

Fix: mirror adopt-or-drop logic in returned_module; enforce policies
transitively over the manifest graph at generation time; wire or
remove `hide`.
