/**
  The licence texts of every library the manylinux wheel bundles, as
  `licenses/<name>/<files>`, copied from the pinned sources the image
  builds (`nix/manylinux/libs.nix` names the same `src`s).

  Each package keeps its own file names (`LICENSE`, `LICENSE_1_0.txt`,
  `COPYING`, `COPYING.GPLv2`, ...), so this copies every top-level
  `LICENSE*`/`COPYING*`/`NOTICE*` file instead of naming one. A source
  with none of them fails the build: its terms are then unknown, and
  the wheel must not ship it silently.

  `extras` names the sources whose terms live in no such file. bdwgc
  states its licence in `README.md`, beside version notes and build
  instructions; the whole file travels, so the terms travel verbatim.

  Nix itself is not in here. Its `COPYING` already travels beside the
  bindings as `COPYING.nix`, with the NOTICE naming its terms.
*/
{
  pkgs,
  lib,
}:
let
  srcs = {
    bzip2 = pkgs.bzip2.src;
    xz = pkgs.xz.src;
    zstd = pkgs.zstd.src;
    brotli = pkgs.brotli.src;
    libsodium = pkgs.libsodium.src;
    sqlite = pkgs.sqlite.src;
    openssl = pkgs.openssl.src;
    nghttp2 = pkgs.nghttp2.src;
    curl = pkgs.curlMinimal.src;
    libarchive = pkgs.libarchive.src;
    libgit2 = pkgs.libgit2.src;
    boost = pkgs.boost.src;
    libblake3 = pkgs.libblake3.src;
    nlohmann_json = pkgs.nlohmann_json.src;
    toml11 = pkgs.toml11.src;
    editline = pkgs.editline.src;
    # The lane adds its own patches on top, which change no terms.
    boehmgc = pkgs.boehmgc.src;
  };
  # Files beyond the globs above, by library: bdwgc states its licence
  # in `README.md`, and keeps no `LICENSE` or `COPYING` file.
  extras = {
    boehmgc = [ "README.md" ];
  };
in
pkgs.runCommand "huggorm-licenses"
  {
    # For the sqlite amalgamation, which ships as a `.zip`.
    nativeBuildInputs = [ pkgs.unzip ];
    # One line per library, for the audit: which source a text came from.
    passthru.sources = srcs;
  }
  ''
    mkdir -p "$out"
    tmp=$(mktemp -d)
    trap 'rm -rf "$tmp"' EXIT
    ${lib.concatStringsSep "\n" (
      lib.mapAttrsToList (name: src: ''
        dest="$out/${name}"
        mkdir -p "$dest"
        if [ -d "${src}" ]; then
          root="${src}"
        else
          stage="$tmp/${name}"
          mkdir -p "$stage"
          (cd "$stage" && unpackFile "${src}" >/dev/null)
          entries=("$stage"/*)
          if [ ''${#entries[@]} -eq 1 ] && [ -d "''${entries[0]}" ]; then
            root="''${entries[0]}"
          else
            root="$stage"
          fi
        fi
        found=0
        for f in "$root"/LICENSE* "$root"/LICENCE* "$root"/COPYING* "$root"/NOTICE*; do
          [ -f "$f" ] || continue
          cp "$f" "$dest/"
          found=1
        done
        ${lib.concatMapStringsSep "\n" (f: ''
          cp "$root"/${f} "$dest/"
          found=1
        '') (extras.${name} or [ ])}
        if [ "$found" -eq 0 ]; then
          echo "${name}: no licence file in ${src}" >&2
          exit 1
        fi
      '') srcs
    )}
    {
      echo "Each directory holds the licence text of one bundled library,"
      echo "copied from the source the manylinux image builds."
      echo ""
      ${lib.concatStringsSep "\n" (lib.mapAttrsToList (name: src: ''echo "${name} ${src}"'') srcs)}
    } > "$out/README"
  ''
