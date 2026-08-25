{
  lib,
  python3Packages,
  fake-library,
  boehmgc,
  nix,
  pkg-config,
  ...
}:
let
  # The Nix libraries this binds, as components rather than the nix
  # package. `nix` is the CLI and drags its whole closure; a binding
  # needs libnixstore and libnixexpr and the two they rest on. Each
  # ships its own pkg-config file, which is what setup.py reads
  # (tasks/015).
  nixLibs = with nix.libs; [
    nix-util
    nix-store
    nix-expr
    nix-fetchers
    nix-flake
  ];
in
python3Packages.buildPythonPackage {
  pname = "cythonix-bindings";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [
    setuptools
    cython
  ];

  # pkg-config finds real Nix. It is how nix ships its build interface:
  # nix-store.pc carries -std=c++23 and a Requires chain that a
  # hand-written include path would have to reconstruct (tasks/015).
  nativeBuildInputs = [ pkg-config ];

  # boehmgc headers must be visible when compiling the extension: the
  # gc-enabled library and every consumer TU must agree on the alias in
  # gc-env.hpp, or implicit destructors get instantiated twice with two
  # different allocators (an ODR split that frees GC memory with free()).
  buildInputs = [
    fake-library
    boehmgc
    # Real Nix, for the first genuine binding. The mock stays until
    # everything it backs has moved across.
  ]
  ++ nixLibs;

  # Propagate fake-library so downstream (cythonix, ourPython)
  # gets the .so at runtime via rpath + propagatedBuildInputs
  propagatedBuildInputs = [ fake-library ] ++ nixLibs;

  # Tell setup.py where to find headers/libs
  env.FAKE_LIBRARY = "${fake-library}";

  # Also ensure the compiler can find it via CFLAGS/LDFLAGS if setup.py didn't
  # (but we already handle it in setup.py)
  # We also need to make the .pxd files available for downstream Cython cimport
  # buildPythonPackage handles that automatically.

  # Don't run `pip check` that might fail
  pythonImportsCheck = [ "cythonix_bindings" ];
}
