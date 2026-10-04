{
  lib,
  python3Packages,
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  boehmgc,
  # The Nix libraries this binds, as components rather than the nix
  # package. `nix` is the CLI and drags its whole closure; a binding
  # needs libnixstore and libnixexpr and the two they rest on. Each
  # ships its own pkg-config file, which is what setup.py reads
  # (huggorm#15). Separate arguments, so a caller with a component
  # scope of its own (nanopynix builds one per Nix version) passes
  # that scope's libraries.
  nix-util,
  nix-store,
  nix-expr,
  nix-fetchers,
  nix-flake,
  # For `lookupFileArg`, which resolves `<nixpkgs>`, `flake:x` and a
  # tarball URL as `nix eval --file` does. It lives in libcmd.
  nix-cmd,
  pkg-config,
  ...
}:
let
  nixLibs = [
    nix-util
    nix-store
    nix-expr
    nix-fetchers
    nix-flake
    nix-cmd
  ];
in
python3Packages.buildPythonPackage {
  pname = "huggorm-bindings";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

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
  # hand-written include path would have to reconstruct (huggorm#15).
  nativeBuildInputs = [ pkg-config ];

  # boehmgc headers must be visible when compiling the extension.
  # `huggorm_decl/cpp/gc.hpp` calls GC_register_my_thread and GC_gcollect
  # directly - libexpr exposes no thread-registration API, so that half
  # of the integration is this repo's. null for a libexpr built without
  # the collector, and then nothing here names a Boehm symbol.
  buildInputs = lib.optional (boehmgc != null) boehmgc ++ nixLibs;

  propagatedBuildInputs = nixLibs;

  # The Nix a declaration's `NIX_VERSION` branch picks an arm for
  # (huggorm#55). Exported, so every build that emits a surface for
  # these bindings describes the same Nix.
  env.HUGGORM_NIX_VERSION = nix-store.version;
  passthru.nixVersion = nix-store.version;
  # Whether libexpr allocates through the collector: its tests ask
  # the build, never the binding they test.
  passthru.hasCollector = boehmgc != null;

  # Nix's own `src/nix/get-env.sh`, which `nix develop` runs as a
  # builder. Nix compiles it into the `nix` binary and no library
  # carries it, so the package that links the libraries carries the
  # copy of the same version. `-f`: no source must fail the build.
  # NOTICE names whose terms the script travels under.
  postInstall = ''
    cp -f "${nix-store.src}/src/nix/get-env.sh" \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/get-env.sh"
    cp -f ${./NOTICE} \
      "$out/${python3Packages.python.sitePackages}/huggorm_bindings/NOTICE"
  '';

  # Don't run `pip check` that might fail
  pythonImportsCheck = [ "huggorm_bindings" ];
}
