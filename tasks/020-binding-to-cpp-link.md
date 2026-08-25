# The C-prefix convention should be a declaration

Found in the 2026-08-25 review.

## Problem

model.map_c_type turns a pxd type into a Python one by stripping a
leading "C" and checking the bindings module for the rest:
CStorePath -> StorePath. That is the ONLY link between
`cdef cppclass CStorePath "fake_library::StorePath"` in the pxd and
`cdef class StorePath` in the pyx. It is a naming convention living in
the generator, invisible from the files it constrains.

Consequences today: a pxd class named anything else silently fails to
map. An unmapped RETURN type is swallowed (returned_types_from_api
catches ValueError and continues, so the class quietly never gets a
wrapper), while an unmapped PARAMETER type raises out of generation.
The asymmetry is accidental.

## Why it matters for real Nix

Real headers will not cooperate with a one-letter convention.
nix::StorePath, nix::ref<Store>, nix::Derivation and friends will need
per-type mapping decisions, and some C++ types will map to no Python
class at all.

## Direction

The pyx declares what it binds, next to the class that binds it:

    cdef class StorePath:
        _binds = "CStorePath"

The generator builds the map from those declarations and hard-fails on
a pxd class no binding claims, or a binding claiming a class the pxd
does not declare. Both directions checked, both at build time. The C
prefix survives as a house style rather than as machinery.

Also fold in: make an unmapped return type as loud as an unmapped
parameter.
