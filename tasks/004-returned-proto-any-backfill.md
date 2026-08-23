# Returned-type protos skip pxd backfill

Review finding 4 (MEDIUM-HIGH, proven). generate.py builds
returned_protos without api=/bindings=, so Derivation.set_env ships
'Any' params: uncallable over the wire while looking alive locally.

Fix: pass api+bindings there too; make residual 'Any' in a manifest a
hard codegen error (or explicit opt-out marker).
