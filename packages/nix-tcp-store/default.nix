/**
  `tcp://host:port`, a Nix store plugin that speaks the daemon protocol
  over TCP. Load it with `plugin-files = <out>/lib/nix/plugins`.

  A plugin is built against the headers of one Nix, so it is a package
  of each lane.
*/
{
  lib,
  stdenv,
  pkg-config,
  nix-util,
  nix-store,
}:
stdenv.mkDerivation {
  pname = "nix-tcp-store";
  inherit (nix-store) version;
  src = lib.fileset.toSource {
    root = ./.;
    fileset = ./tcp-store.cc;
  };

  nativeBuildInputs = [ pkg-config ];
  buildInputs = [
    nix-util
    nix-store
  ];

  # Linked against libnixstore, which Nix's plugin documentation advises
  # against for the `nix` CLI. A Python process loads libnixstore with
  # RTLD_LOCAL, so a plugin that leaves the symbols undefined does not
  # resolve there. The library is the lane's own, so the loader finds the
  # one already loaded.
  buildPhase = ''
    runHook preBuild
    flags=()
    if grep -q FilePathType "$(pkg-config --variable=includedir nix-store)/nix/store/remote-store.hh"; then
      flags+=(-DNIX_TCP_STORE_FILE_PATH_TYPE)
    fi
    $CXX -std=c++23 -O2 -fPIC -shared -Wall -Wextra -Werror "''${flags[@]}" \
      $(pkg-config --cflags nix-store) tcp-store.cc \
      $(pkg-config --libs nix-store) -o libnix-tcp-store.so
    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    install -Dm755 libnix-tcp-store.so "$out/lib/nix/plugins/libnix-tcp-store.so"
    runHook postInstall
  '';
}
