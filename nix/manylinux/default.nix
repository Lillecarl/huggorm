/**
  huggorm-bindings as `manylinux_2_28` wheels, one per CPython.

  PyPA's own manylinux image does the compiling: its gcc-toolset links
  the parts of libstdc++ that glibc 2.28's libstdc++ lacks statically,
  and its glibc is 2.28. So the floor comes from the toolchain, and
  nothing rewrites a symbol afterwards.

  The image runs in the build sandbox under bwrap, with the store
  mounted read-only beside it. nixpkgs pins every source, and the
  patched Nix is the one the rest of huggorm binds.

  A scope: each library is a member, and a member's arguments resolve
  to other members first, so `curl` takes this scope's `openssl`.
  `overrideScope` replaces one library for everything above it.
*/
{
  pkgs,
  lib,
  # huggorm's patched Nix, from `patchNix`, and the collector it links.
  nix,
  boehmgc,
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
  nixpkgsBoehmgc = boehmgc;

  # The image digest is the manifest of one architecture, not the index,
  # so `skopeo inspect --raw` on the tag gives it.
  images = {
    x86_64-linux = {
      imageName = "quay.io/pypa/manylinux_2_28_x86_64";
      imageDigest = "sha256:c2261579b9c2e5d45aa93312f73e2a302182e3e977b558581a1838d6fed3d8e6";
      hash = "sha256-oynPv331pbE5hEYZrgvbHSnNXGlt/N5Sh+z/ARntVYg=";
      finalImageTag = "2026-09-30";
    };
  };

  # Every CPython in the image that the bindings' requires-python allows.
  pythons = [
    "cp311"
    "cp312"
    "cp313"
    "cp314"
  ];
  interpreter = py: "/opt/python/${py}-${py}/bin/python3";

  # The closure of nixpkgs' Python packages, without the interpreter.
  # `requiredPythonModules` also returns nixpkgs' python3, and its
  # site-packages carries a `_sysconfigdata` and a `sitecustomize.py`
  # that replace the image interpreter's: SOABI read `cpython-314` under
  # 3.13. Only a package has `pythonModule`.
  pythonClosure =
    packages: lib.filter (p: p ? pythonModule) (pkgs.python3.pkgs.requiredPythonModules packages);
  # A PYTHONPATH for an image interpreter, from a closure as above.
  pythonPath = lib.concatMapStringsSep ":" (p: "${p}/${pkgs.python3.sitePackages}");
in
lib.makeScope pkgs.newScope (self: {
  # skopeo reads `$XDG_RUNTIME_DIR/containers/auth.json`, and without the
  # variable it reads `/run/containers/<uid>`, which the sandbox refuses.
  image = (pkgs.dockerTools.pullImage images.${pkgs.stdenv.hostPlatform.system}).overrideAttrs {
    XDG_RUNTIME_DIR = "/build";
  };

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

    `deps` are other members. Each one's prefix, and its own deps'
    prefixes, reach the compiler, the linker, pkg-config and cmake.
    `script` runs under the image's bash, in the unpacked source.
  */
  build =
    {
      pname,
      version ? "",
      src,
      deps ? [ ],
      patches ? [ ],
      patchFlags ? [ "-p1" ],
      # nixpkgs programs that win over the image's own.
      tools ? [ ],
      script,
    }:
    let
      closure = lib.unique (lib.concatMap (d: d.passthru.closure) deps);
      join = sep: f: lib.concatMapStringsSep sep f closure;
      env = {
        PATH = lib.concatStringsSep ":" [
          "/opt/rh/gcc-toolset-14/root/usr/bin"
          (lib.makeBinPath tools)
          "/usr/local/bin"
          "/usr/bin"
          "/bin"
          "${self.meson}/bin"
          "${pkgs.ninja}/bin"
          "${pkgs.flex}/bin"
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
      setenv = lib.concatStrings (
        lib.mapAttrsToList (name: value: " --setenv ${name} ${lib.escapeShellArg value}") env
      );
      runner = pkgs.writeText "${pname}-build.sh" ''
        set -euo pipefail
        cd "$SRC"
        for patch in ${lib.concatMapStringsSep " " (p: "${p}") patches}; do
          patch ${lib.escapeShellArgs patchFlags} < "$patch"
        done
        ${script}
      '';
      drv =
        pkgs.runCommand "manylinux-${pname}${lib.optionalString (version != "") "-${version}"}"
          {
            nativeBuildInputs = [
              pkgs.bubblewrap
              pkgs.unzip
            ];
            passthru = {
              inherit pname version;
              closure = closure ++ [ drv ];
            };
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
              --clearenv${setenv} \
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

  # Each library Nix links, from the source nixpkgs pins, with every
  # optional feature off that Nix does not use.
  #
  # zlib is the image's own: `libz.so.1` is on the manylinux policy's
  # list of libraries a host provides, so auditwheel never bundles it.

  # nixpkgs' second patch turns bzip2's Makefile into autotools.
  bzip2 = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.bzip2)
        pname
        version
        src
        patches
        patchFlags
        ;
      script = "autoreconf -fi\n" + autotools "";
    }
  ) { };
  xz = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.xz) pname version src;
      script = autotools "--disable-doc --disable-scripts --disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo --disable-lzma-links";
    }
  ) { };
  zstd = self.callPackage (
    { build }:
    build {
      inherit (pkgs.zstd) pname version src;
      script = ''
        make -C lib -j"$NIX_BUILD_CORES" libzstd PREFIX="$out"
        make -C lib install-pc install-includes install-shared PREFIX="$out" LIBDIR="$out/lib"
      '';
    }
  ) { };
  brotli = self.callPackage (
    { build, cmake }:
    build {
      inherit (pkgs.brotli) pname version src;
      script = cmake "-DBROTLI_BUILD_TOOLS=OFF";
    }
  ) { };
  libsodium = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.libsodium) pname version src;
      script = "[ -x configure ] || ./autogen.sh -s\n" + autotools "";
    }
  ) { };
  sqlite = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.sqlite) pname version src;
      script = autotools "--disable-tcl";
    }
  ) { };
  openssl = self.callPackage (
    { build }:
    build {
      inherit (pkgs.openssl) pname version src;
      # Configure needs IPC::Cmd, which the image's perl lacks.
      tools = [ pkgs.perl ];
      script = ''
        ./Configure --prefix="$out" --libdir=lib shared no-tests no-docs no-apps
        make -j"$NIX_BUILD_CORES"
        make install_sw
      '';
    }
  ) { };
  nghttp2 = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.nghttp2) pname version src;
      script = autotools "--enable-lib-only";
    }
  ) { };
  curl = self.callPackage (
    {
      build,
      autotools,
      openssl,
      nghttp2,
      zstd,
      brotli,
    }:
    build {
      inherit (pkgs.curlMinimal) pname version src;
      deps = [
        openssl
        nghttp2
        zstd
        brotli
      ];
      script = autotools ''
        --with-openssl --with-nghttp2 --with-zlib --with-zstd --with-brotli \
        --without-libpsl --without-libidn2 --without-libssh2 \
        --disable-ldap --disable-ldaps --disable-rtsp --disable-dict --disable-telnet \
        --disable-tftp --disable-pop3 --disable-imap --disable-smtp --disable-gopher \
        --disable-mqtt --disable-manual --disable-docs
      '';
    }
  ) { };
  libarchive = self.callPackage (
    {
      build,
      cmake,
      bzip2,
      xz,
      zstd,
    }:
    build {
      inherit (pkgs.libarchive) pname version src;
      deps = [
        bzip2
        xz
        zstd
      ];
      script = cmake ''
        -DENABLE_OPENSSL=OFF -DENABLE_LIBXML2=OFF -DENABLE_EXPAT=OFF -DENABLE_LZ4=OFF \
        -DENABLE_LIBB2=OFF -DENABLE_ACL=OFF -DENABLE_TEST=OFF -DENABLE_TAR=OFF \
        -DENABLE_CPIO=OFF -DENABLE_CAT=OFF -DENABLE_UNZIP=OFF -DENABLE_WERROR=OFF
      '';
    }
  ) { };
  libgit2 = self.callPackage (
    {
      build,
      cmake,
      openssl,
    }:
    build {
      inherit (pkgs.libgit2) pname version src;
      deps = [ openssl ];
      script = cmake ''
        -DUSE_SSH=OFF -DUSE_HTTPS=OpenSSL -DUSE_GSSAPI=OFF -DREGEX_BACKEND=builtin \
        -DUSE_HTTP_PARSER=builtin -DBUILD_TESTS=OFF -DBUILD_CLI=OFF
      '';
    }
  ) { };
  # The modules Nix's meson files name, and not the rest of boost.
  boost = self.callPackage (
    { build }:
    build {
      inherit (pkgs.nixDependencies.boost) pname version src;
      script = ''
        ./bootstrap.sh --prefix="$out" --with-libraries=context,coroutine,iostreams,url,container,system,thread
        ./b2 -j"$NIX_BUILD_CORES" install link=shared variant=release threading=multi \
          runtime-link=shared -sNO_ZLIB=1 -sNO_BZIP2=1 -sNO_LZMA=1 -sNO_ZSTD=1 cxxflags=-fPIC
      '';
    }
  ) { };
  libblake3 = self.callPackage (
    { build, cmake }:
    build {
      inherit (pkgs.libblake3) pname version src;
      script = "cd c\n" + cmake "-DBLAKE3_USE_TBB=OFF";
    }
  ) { };
  nlohmann_json = self.callPackage (
    { build, cmake }:
    build {
      inherit (pkgs.nlohmann_json) pname version src;
      script = cmake "-DJSON_BuildTests=OFF";
    }
  ) { };
  toml11 = self.callPackage (
    { build, cmake }:
    build {
      inherit (pkgs.toml11) pname version src;
      script = cmake "";
    }
  ) { };
  editline = self.callPackage (
    { build, autotools }:
    build {
      inherit (pkgs.editline) pname version src;
      script = "./autogen.sh\n" + autotools "";
    }
  ) { };
  # The collector libexpr links in the rest of huggorm, with its flags
  # and its patches.
  boehmgc = self.callPackage (
    { build, autotools }:
    build {
      inherit (nixpkgsBoehmgc)
        pname
        version
        src
        patches
        ;
      script = "./autogen.sh\n" + autotools (lib.escapeShellArgs nixpkgsBoehmgc.configureFlags);
    }
  ) { };

  # Nix's libraries, each its own meson project as nixpkgs builds them,
  # from the source huggorm patches. No CLI, no tests. The features off
  # here are the ones the rest of huggorm's Nix has and this one lacks:
  # seccomp, libcpuid, lowdown and AWS authentication for S3.
  nixLibs = self.callPackage (
    {
      build,
      bzip2,
      xz,
      zstd,
      brotli,
      libsodium,
      sqlite,
      openssl,
      nghttp2,
      curl,
      libarchive,
      libgit2,
      boost,
      libblake3,
      nlohmann_json,
      toml11,
      editline,
      boehmgc,
    }:
    let
      components = {
        libutil = "-Dcpuid=disabled";
        libstore = "-Dseccomp-sandboxing=disabled -Ds3-aws-auth=disabled";
        libfetchers = "";
        libexpr = "-Dgc=enabled";
        libflake = "";
        libmain = "";
        libcmd = "-Dmarkdown=disabled -Dreadline-flavor=editline";
      };
      # Each one links the ones before it.
      order = [
        "libutil"
        "libstore"
        "libfetchers"
        "libexpr"
        "libflake"
        "libmain"
        "libcmd"
      ];
    in
    build {
      pname = "nix";
      inherit (nix) version;
      inherit (nix.libs.nix-util) src;
      deps = [
        bzip2
        xz
        zstd
        brotli
        libsodium
        sqlite
        openssl
        nghttp2
        curl
        libarchive
        libgit2
        boost
        libblake3
        nlohmann_json
        toml11
        editline
        boehmgc
      ];
      # The parser asks for `parse.error detailed`, bison 3.6; the image has 3.0.4.
      tools = [ pkgs.bison ];
      script = ''
        export PKG_CONFIG_PATH="$out/lib/pkgconfig:$PKG_CONFIG_PATH"
        export LD_LIBRARY_PATH="$out/lib:$LD_LIBRARY_PATH"
        export BOOST_ROOT=${boost}
        # `preloadNSS` calls dlopen, which glibc keeps in libdl before 2.34
        # and Nix's meson files never name.
        export LDFLAGS="$LDFLAGS -ldl"
      ''
      + lib.concatMapStrings (name: ''
        meson setup _build/${name} src/${name} --prefix="$out" --libdir=lib \
          --buildtype=release ${components.${name}}
        meson compile -C _build/${name}
        meson install -C _build/${name}
      '') order;
    }
  ) { };

  /**
    The extension for one CPython, built by the package's own setup.py
    under that interpreter, then repaired: auditwheel copies every
    library it links into the wheel under a hashed name, and refuses
    the tag if any object needs more than glibc 2.28.

    Every wheel compiles the one emitted tree, `bindings-src`. The
    generator reads declarations through `annotationlib`, which is
    Python 3.14, and one tree means one C++ for every interpreter.
  */
  wheelFor =
    py:
    self.callPackage (
      { build, nixLibs }:
      build {
        pname = "huggorm-bindings-${py}";
        inherit (nix) version;
        src = ../../packages/huggorm-bindings;
        deps = [ nixLibs ];
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
          cp ${nix.libs.nix-store.src}/src/nix/get-env.sh huggorm_bindings/get-env.sh
          ${interpreter py} -m pip wheel --no-build-isolation \
            --no-deps --no-index --wheel-dir dist .
          auditwheel repair --plat manylinux_2_28_x86_64 --wheel-dir "$out" dist/*.whl
        '';
      }
    ) { };
  wheels = lib.genAttrs pythons self.wheelFor;

  /**
    What one wheel must do, under its own interpreter, inside the image.

    The wheel is the only compiled code under test: pip installs it
    into the image's CPython, and the pure Python around it comes from
    nixpkgs with nixpkgs' bindings filtered out. A nixpkgs C extension
    built against glibc 2.42 would fail to load here, so protobuf and
    multidict take their pure Python implementations.

    Every wheel runs `smoke.py`. cp314 also runs the hermetic suite,
    which imports the generator and so runs nowhere else.
  */
  checkFor =
    py:
    let
      python = interpreter py;
      # nixpkgs' bindings are the one package left out: the wheel's are under test.
      pure = lib.filter (p: (p.pname or "") != "huggorm-bindings") (
        pythonClosure (
          [ huggorm ]
          ++ (with pkgs.python3.pkgs; [
            pytest
            anyio
            pytest-timeout
            # anyio needs it below 3.13, and nixpkgs' 3.14 build drops it.
            typing-extensions
          ])
          ++ lib.optional (py == "cp314") huggorm-gen
        )
      );
      generator = pkgs.python3.withPackages (_: [ huggorm-gen ]);
    in
    self.build {
      pname = "huggorm-wheel-check-${py}";
      inherit (nix) version;
      src = ../../packages/huggorm;
      script = ''
        ${generator}/bin/python3 -c \
          'from huggorm_gen.cppgen.generate import nanobind_modules; print(*nanobind_modules())' \
          > /tmp/modules
        ${python} -m pip install --no-index --no-deps --target /tmp/site ${self.wheels.${py}}/*.whl
        export PYTHONPATH=/tmp/site:${pythonPath pure}
        export PATH="$PATH:${pkgs.grpcurl}/bin"
        export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python MULTIDICT_NO_EXTENSIONS=1
        export HUGGORM_NIX_VERSION=${lib.escapeShellArg nix.libs.nix-store.version} HUGGORM_NIX_GC=1
        ${python} ${./smoke.py} /tmp/modules | tee "$out/smoke.log"
      ''
      + lib.optionalString (py == "cp314") ''
        ${python} -m pytest -p no:cacheprovider -m "not live" tests 2>&1 | tee "$out/pytest.log"
      '';
    };
  checks = lib.genAttrs pythons self.checkFor;

  # The module list the generator gives, one per line: what `smoke.py`
  # holds a wheel to.
  modules =
    pkgs.runCommand "huggorm-bindings-modules"
      { nativeBuildInputs = [ (pkgs.python3.withPackages (_: [ huggorm-gen ])) ]; }
      ''
        python3 -c 'from huggorm_gen.cppgen.generate import nanobind_modules; print(*nanobind_modules(), sep="\n")' > $out
      '';

  /**
    Every wheel `pip install --no-index --find-links` needs for huggorm:
    the bindings for each CPython, and a pure wheel of each Python
    dependency, from nixpkgs' `dist` output.

    nixpkgs builds protobuf and multidict with their C extensions, for
    3.14 alone. PyPI publishes a pure wheel of the same versions, which
    is the one pip takes on any other interpreter.
  */
  wheelhouse =
    let
      fromPyPI = {
        protobuf = {
          url = "https://files.pythonhosted.org/packages/39/ca/c47f91d3cab175b01fd8c4f0d80fdf8613be876cc616e66ad281a59c5ddf/protobuf-7.36.1-py3-none-any.whl";
          sha256 = "7d951e46b3f963d6c264c367c437921de9d5aedd9c3f9612b9077736b4e3ad5c";
        };
        multidict = {
          url = "https://files.pythonhosted.org/packages/81/08/7036c080d7117f28a4af526d794aab6a84463126db031b007717c1a6676e/multidict-6.7.1-py3-none-any.whl";
          sha256 = "55d97cc6dae627efa6a6e548885712d4864b81110ac76fa4e534c03819fa4a56";
        };
      };
      pinned =
        name:
        let
          version = pkgs.python3.pkgs.${name}.version;
          wheel = fromPyPI.${name};
        in
        if lib.hasInfix "-${version}-" wheel.url then
          pkgs.fetchurl wheel
        else
          throw "nixpkgs' ${name} is ${version}; pin its pure wheel from PyPI in nix/manylinux";
      dists = map (p: p.dist) (
        lib.filter (p: !(lib.elem p.pname ([ "huggorm-bindings" ] ++ lib.attrNames fromPyPI)))
          (pythonClosure [
            huggorm
            # anyio needs it below 3.13.
            pkgs.python3.pkgs.typing-extensions
          ])
      );
    in
    pkgs.runCommand "huggorm-wheelhouse" { } ''
      mkdir $out
      cp ${lib.concatMapStringsSep " " (w: "${w}/*.whl") (lib.attrValues self.wheels)} $out/
      cp ${lib.concatMapStringsSep " " (d: "${d}/*.whl") dists} $out/
      ${lib.concatMapStrings (name: ''
        cp ${pinned name} $out/${baseNameOf fromPyPI.${name}.url}
      '') (lib.attrNames fromPyPI)}
    '';

  /**
    huggorm on openSUSE Leap 16.0's own Python, from the wheelhouse.

    The guest is SUSE's cloud image under vivarium. `suse.py` makes a
    venv on its python3 and installs huggorm with pip, offline: what a
    host without Nix does.
  */
  suse = self.callPackage (
    { wheelhouse, modules }:
    vivarium.mkTest {
      name = "huggorm-suse";
      settings = {
        wheelhouse = "${wheelhouse}";
        modules = "${modules}";
        smoke = "${./smoke.py}";
        closure = "${pkgs.closureInfo {
          rootPaths = [
            wheelhouse
            modules
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
    }
  ) { };
})
