{
  lib,
  python3Packages,
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  boehmgc,
  nix,
  pkg-config,
  # The source tree to compile. NOT ./. any more: no module in here
  # has hand-written source, and every one of them is C++ emitted
  # from a declaration before the build starts. The root default.nix
  # builds that tree and passes it in.
  src,
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
  pname = "huggorm-bindings";
  version = "0.1.0";
  pyproject = true;
  inherit src;

  build-system = with python3Packages; [
    setuptools
    # nanobind, and the declarations. There is no Cython here at all:
    # every module is C++ written from a declaration before this
    # builds, and setup.py reads the module list out of huggorm-decl
    # rather than naming them again.
    nanobind
    huggorm-gen
    huggorm-decl
    huggorm-dsl
  ];

  # pkg-config finds real Nix. It is how nix ships its build interface:
  # nix-store.pc carries -std=c++23 and a Requires chain that a
  # hand-written include path would have to reconstruct (tasks/015).
  nativeBuildInputs = [ pkg-config ];

  # boehmgc headers must be visible when compiling the extension.
  # `_cpp/eval.hpp` calls GC_register_my_thread and GC_gcollect
  # directly - libexpr exposes no thread-registration API, so that half
  # of the integration is this repo's.
  buildInputs = [ boehmgc ] ++ nixLibs;

  propagatedBuildInputs = nixLibs;

  # Don't run `pip check` that might fail
  pythonImportsCheck = [ "huggorm_bindings" ];
}
