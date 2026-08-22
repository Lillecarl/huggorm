{
  lib,
  stdenv,
  meson,
  ninja,
  pkg-config,
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
}
