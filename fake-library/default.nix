{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
  boehmgc,
}:
stdenv.mkDerivation {
  pname = "fake-library";
  version = "0.1.0";
  src = ./.;

  nativeBuildInputs = [
    meson
    ninja
    pkg-config
  ];
  buildInputs = [ boehmgc ];
}
