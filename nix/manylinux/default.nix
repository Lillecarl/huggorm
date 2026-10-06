/**
  huggorm-bindings as `manylinux_2_28` wheels, one per CPython.

  PyPA's own manylinux image does the compiling: its gcc-toolset links
  the parts of libstdc++ that glibc 2.28's libstdc++ lacks statically,
  and its glibc is 2.28. So the floor comes from the toolchain, and
  nothing rewrites a symbol afterwards.

  The image runs in the build sandbox under bwrap, with the store
  mounted read-only beside it. nixpkgs pins every source, and the
  patched Nix is the one the rest of huggorm binds.

  A scope: `overrideScope` replaces one library for everything above it.
*/
{
  pkgs,
  lib,
  # A lane's patched Nix components, and the collector libexpr links.
  nixComponents,
  boehmgc,
  # `get-env.sh` as the `nix` binary embeds it.
  getEnvSh,
  # The emitted C++ for that Nix, which every wheel compiles.
  bindings-src,
  # The generator, for the module list a check holds a wheel to, and
  # the declarations, for the helper headers the emitted C++ includes.
  huggorm-gen,
  huggorm-decl,
  # The pure Python layer, which the checks run over each wheel.
  huggorm,
  # vivarium's lib.nix, for the test on openSUSE.
  vivarium,
}:
let
  inherit (pkgs.stdenv.hostPlatform) system;
  platform = "manylinux_2_28_${pkgs.stdenv.hostPlatform.parsed.cpu.name}";

  # The digest is the manifest of one architecture, not the index:
  # `skopeo inspect --raw` on the tag gives it.
  images.x86_64-linux = {
    imageName = "quay.io/pypa/manylinux_2_28_x86_64";
    imageDigest = "sha256:c2261579b9c2e5d45aa93312f73e2a302182e3e977b558581a1838d6fed3d8e6";
    hash = "sha256-oynPv331pbE5hEYZrgvbHSnNXGlt/N5Sh+z/ARntVYg=";
    finalImageTag = "2026-09-30";
  };

  # Every CPython in the image that the runtime packages' requires-python allows.
  pythons = [
    "cp311"
    "cp312"
    "cp313"
    "cp314"
  ];
  interpreter = py: "/opt/python/${py}-${py}/bin/python3";

  # The closure of nixpkgs' Python packages, without the interpreter.
  # `requiredPythonModules` also returns nixpkgs' python3, whose
  # site-packages holds a `_sysconfigdata` and a `sitecustomize.py` that
  # replace an image interpreter's: SOABI read `cpython-314` under 3.13.
  # Only a package has `pythonModule`.
  pythonClosure =
    packages: lib.filter (p: p ? pythonModule) (pkgs.python3.pkgs.requiredPythonModules packages);
  pythonPath = lib.concatMapStringsSep ":" (p: "${p}/${pkgs.python3.sitePackages}");

  libs = import ./libs.nix {
    inherit
      pkgs
      lib
      nixComponents
      boehmgc
      ;
  };
in
lib.makeScope pkgs.newScope (
  self:
  lib.mapAttrs (_: f: self.callPackage f { }) libs.libraries
  // {
    # skopeo reads `$XDG_RUNTIME_DIR/containers/auth.json`, and without the
    # variable it reads `/run/containers/<uid>`, which the sandbox refuses.
    image = (pkgs.dockerTools.pullImage images.${system}).overrideAttrs { XDG_RUNTIME_DIR = "/build"; };

    rootfs = pkgs.runCommand "manylinux_2_28-rootfs" { nativeBuildInputs = [ pkgs.python3 ]; } ''
      python3 ${./rootfs.py} ${self.image} $out
    '';

    # meson from its own source, under the image's Python: nixpkgs' meson
    # carries patches that change how it searches for boost and rpaths.
    meson = pkgs.writeTextFile {
      name = "meson";
      executable = true;
      destination = "/bin/meson";
      text = ''
        #!/bin/sh
        exec ${interpreter "cp312"} ${pkgs.meson.src}/meson.py "$@"
      '';
    };

    /**
      Build `src` inside the image, into `$out`.

      Each of `deps`, and each of theirs, reaches the compiler, the
      linker, pkg-config and cmake. `tools` are nixpkgs programs that
      win over the image's own. `script` runs under the image's bash, in
      the unpacked source.
    */
    build =
      {
        pname,
        version,
        src,
        deps ? [ ],
        patches ? [ ],
        patchFlags ? [ "-p1" ],
        tools ? [ ],
        script,
      }:
      let
        closure = lib.unique (lib.concatMap (d: d.closure) deps);
        join = sep: f: lib.concatMapStringsSep sep f closure;
        env = {
          PATH = lib.concatStringsSep ":" [
            "/opt/rh/gcc-toolset-14/root/usr/bin"
            (lib.makeBinPath tools)
            "/usr/local/bin"
            "/usr/bin"
            "/bin"
            (lib.makeBinPath [
              self.meson
              pkgs.ninja
              pkgs.flex
            ])
          ];
          PKG_CONFIG_PATH = join ":" (d: "${d}/lib/pkgconfig:${d}/share/pkgconfig");
          CMAKE_PREFIX_PATH = join ":" toString;
          # -isystem, not -I: Nix builds with -Werror, and an -I here would
          # turn a warning inside boost's headers into Nix's error.
          CPPFLAGS = join " " (d: "-isystem ${d}/include");
          LDFLAGS = join " " (d: "-L${d}/lib");
          # The linker follows a library's own NEEDED entries through this,
          # and a configure check runs what it links.
          LD_LIBRARY_PATH = join ":" (d: "${d}/lib");
          CFLAGS = "-O2 -fPIC";
          CXXFLAGS = "-O2 -fPIC";
          HOME = "/tmp";
        };
        runner = pkgs.writeText "${pname}-build.sh" ''
          set -euo pipefail
          cd "$SRC"
          for patch in ${lib.concatMapStringsSep " " (p: "${p}") patches}; do
            patch ${lib.escapeShellArgs patchFlags} < "$patch"
          done
          ${script}
        '';
        drv =
          pkgs.runCommand "manylinux-${pname}-${version}"
            {
              nativeBuildInputs = [
                pkgs.bubblewrap
                pkgs.unzip
              ];
              passthru.closure = closure ++ [ drv ];
            }
            ''
              mkdir -p "$out" unpack
              cd unpack
              unpackFile ${src}
              chmod -R u+w .
              src=$(echo "$PWD"/*)
              cd ..
              bwrap --ro-bind ${self.rootfs} / --dev /dev --proc /proc --tmpfs /tmp \
                --ro-bind /nix/store /nix/store --bind "$out" "$out" \
                --bind "$NIX_BUILD_TOP" "$NIX_BUILD_TOP" \
                --clearenv ${
                  lib.concatStringsSep " " (
                    lib.mapAttrsToList (name: value: "--setenv ${name} ${lib.escapeShellArg value}") env
                  )
                } \
                --setenv out "$out" --setenv SRC "$src" \
                --setenv NIX_BUILD_CORES "$NIX_BUILD_CORES" \
                /bin/bash ${runner}
            '';
      in
      drv;

    autotools = flags: ''
      ./configure --prefix="$out" --libdir="$out/lib" --disable-static ${flags}
      make -j"$NIX_BUILD_CORES"
      make install
    '';
    cmake = flags: ''
      cmake -S . -B _build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$out" \
        -DCMAKE_INSTALL_LIBDIR=lib -DBUILD_SHARED_LIBS=ON ${flags}
      cmake --build _build -j"$NIX_BUILD_CORES"
      cmake --install _build
    '';

    nixLibs = self.callPackage libs.nixLibs {
      deps = map (name: self.${name}) (lib.attrNames libs.libraries);
    };

    /**
      The extension for one CPython, built by the package's own setup.py
      under that interpreter, then repaired: auditwheel copies every
      library it links into the wheel under a hashed name, and refuses
      the tag if any object needs more than glibc 2.28.

      Every wheel compiles the one emitted tree, `bindings-src`. The
      generator reads declarations through `annotationlib`, which is
      Python 3.14, and one tree means one C++ for every interpreter.
    */
    wheels = lib.genAttrs pythons (
      py:
      self.build {
        pname = "huggorm-bindings-${py}";
        inherit (nixComponents) version;
        src = ../../packages/huggorm-bindings;
        deps = [ self.nixLibs ];
        script = ''
          export PYTHONPATH=${
            pythonPath (pythonClosure [
              pkgs.python3.pkgs.setuptools
              pkgs.python3.pkgs.nanobind
            ])
          }
          export HUGGORM_BINDINGS_EMITTED=${bindings-src}
          export HUGGORM_DECL_INCLUDE=${huggorm-decl}/${pkgs.python3.sitePackages}
          mkdir -p huggorm_bindings
          cp ${getEnvSh} huggorm_bindings/get-env.sh
          cp ${nixComponents.nix-store.src}/COPYING huggorm_bindings/COPYING.nix
          cp ${../../packages/huggorm-bindings/NOTICE} huggorm_bindings/NOTICE
          ${interpreter py} -m pip wheel --no-build-isolation \
            --no-deps --no-index --wheel-dir dist .
          auditwheel repair --plat ${platform} --wheel-dir "$out" dist/*.whl
        '';
      }
    );

    # The module list the generator gives, one per line: what `smoke.py`
    # holds a wheel to.
    modules =
      pkgs.runCommand "huggorm-bindings-modules"
        { nativeBuildInputs = [ (pkgs.python3.withPackages (_: [ huggorm-gen ])) ]; }
        ''
          python3 -c 'from huggorm_gen.cppgen.generate import nanobind_modules; print(*nanobind_modules(), sep="\n")' > $out
        '';

    /**
      Runs `script` against one wheel, under its own interpreter, inside
      the image.

      The wheel is the only compiled code under test: pip installs it
      into the image's CPython, and the pure Python around it comes from
      nixpkgs with nixpkgs' bindings filtered out. A nixpkgs C extension
      built against glibc 2.42 would fail to load here, so msgpack
      takes its pure Python implementation.
    */
    wheelCheck =
      {
        pname,
        py,
        script,
        extraPythonPackages ? [ ],
      }:
      let
        # The wheel's bindings are under test, so nixpkgs' are left out.
        pure = lib.filter (p: p.pname != "huggorm-bindings") (
          pythonClosure (
            [
              huggorm
              pkgs.python3.pkgs.pytest
              pkgs.python3.pkgs.pytest-timeout
              # anyio needs it below 3.13, and nixpkgs' 3.14 build drops it.
              pkgs.python3.pkgs.typing-extensions
            ]
            ++ extraPythonPackages
          )
        );
      in
      self.build {
        inherit pname;
        inherit (nixComponents) version;
        src = ../../packages/huggorm;
        script = ''
          ${interpreter py} -m pip install --no-index --no-deps --target /tmp/site ${self.wheels.${py}}/*.whl
          export PYTHONPATH=/tmp/site:${pythonPath pure}
          export MSGPACK_PUREPYTHON=1
          export HUGGORM_NIX_VERSION=${lib.escapeShellArg nixComponents.version} HUGGORM_NIX_GC=1
        ''
        + script;
      };

    # `smoke.py` against every wheel.
    checks = lib.genAttrs pythons (
      py:
      self.wheelCheck {
        pname = "huggorm-wheel-check-${py}";
        inherit py;
        script = ''
          ${interpreter py} ${./smoke.py} ${self.modules} | tee "$out/smoke.log"
        '';
      }
    );

    # The hermetic suite against the cp314 wheel. It imports the
    # generator, which needs 3.14.
    suite = self.wheelCheck {
      pname = "huggorm-wheel-suite-cp314";
      py = "cp314";
      extraPythonPackages = [ huggorm-gen ];
      script = ''
        ${interpreter "cp314"} -m pytest -p no:cacheprovider -m "not live" tests 2>&1 | tee "$out/pytest.log"
      '';
    };

    /**
      Every wheel `pip install --no-index --find-links` needs for huggorm:
      the bindings for each CPython, and a pure wheel of each Python
      dependency, from nixpkgs' `dist` output.

      nixpkgs builds msgpack with its C extension, for 3.14 alone, and
      PyPI publishes no pure wheel of it. So this pins the manylinux
      wheel for each CPython this lane builds for. The pins are for
      this offline install alone; the packages' metadata declares
      floors.
    */
    wheelhouse =
      let
        fromPyPI = {
          msgpack = [
            {
              url = "https://files.pythonhosted.org/packages/03/8d/671d81534ea0e2b0e8a121be100020da09eb78861fe3aa8f3ef7dcd3bed1/msgpack-1.2.1-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
              sha256 = "a28d076ca7c82b9c8728ad90b7147489449557038bed50e4241eb832395169b4";
            }
            {
              url = "https://files.pythonhosted.org/packages/6a/fd/6adabd4f6d5e686f97dd02ce7fce3fe4cf672cbac36b8f67ff4040e8ad8b/msgpack-1.2.1-cp312-cp312-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
              sha256 = "020e881a764b20d8d7ca1a54fc01b8175519d108e3c3f194fddc200bda95951a";
            }
            {
              url = "https://files.pythonhosted.org/packages/79/d3/36a46a8ed992b781acbc05928bd5bee3c810cb0c3563bf81a7b0c04a1a76/msgpack-1.2.1-cp313-cp313-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
              sha256 = "787c9bebb5833e8f6fc8abca3c0597683d8d87f56a8842b6b89c75a5f3176e2d";
            }
            {
              url = "https://files.pythonhosted.org/packages/19/03/8c63e8cf52958534ef688625965ab04c269a6cadd8caef16758b380a821a/msgpack-1.2.1-cp314-cp314-manylinux2014_x86_64.manylinux_2_17_x86_64.manylinux_2_28_x86_64.whl";
              sha256 = "0e2bf9280bceb5efca998435904b5d3e9fdbcc11d90dc9df30aec7973252b720";
            }
          ];
        };
        pinned = lib.concatLists (
          lib.mapAttrsToList (
            name: wheels:
            let
              inherit (pkgs.python3.pkgs.${name}) version;
            in
            map (
              wheel:
              if lib.hasInfix "-${version}-" wheel.url then
                pkgs.fetchurl wheel
              else
                throw "nixpkgs' ${name} is ${version}; pin its wheels from PyPI in nix/manylinux"
            ) wheels
          ) fromPyPI
        );
        dists = map (p: p.dist) (
          lib.filter (p: !lib.elem p.pname ([ "huggorm-bindings" ] ++ lib.attrNames fromPyPI))
            (pythonClosure [
              huggorm
              # anyio needs it below 3.13.
              pkgs.python3.pkgs.typing-extensions
            ])
        );
      in
      pkgs.runCommand "huggorm-wheelhouse" { } ''
        mkdir $out
        cp ${lib.concatMapStringsSep " " (w: "${w}/*.whl") (lib.attrValues self.wheels ++ dists)} $out/
        ${lib.concatMapStrings (w: "cp ${w} $out/${w.name}\n") pinned}
      '';

    /**
      huggorm on openSUSE Leap 16.0's own Python, from the wheelhouse.

      The guest is SUSE's cloud image under vivarium. `suse.py` makes a
      venv on its python3 and installs huggorm with pip, offline: what a
      host without Nix does.
    */
    suse = vivarium.mkTest {
      name = "huggorm-suse";
      settings = {
        wheelhouse = "${self.wheelhouse}";
        modules = "${self.modules}";
        smoke = "${./smoke.py}";
        closure = "${pkgs.closureInfo {
          rootPaths = [
            self.wheelhouse
            self.modules
            ./smoke.py
          ];
        }}";
      };
      nodes.suse = {
        vivarium.image = vivarium.images.opensuse-leap-16_0;
        vivarium.memory = "1024M";
      };
      phases.huggorm = {
        script = ./suse.py;
        after = [ "boot" ];
      };
    };
  }
)
