/**
  huggorm-bindings as a `manylinux_2_28` wheel.

  PyPA's own manylinux image does the compiling: its gcc-toolset links
  the parts of libstdc++ that glibc 2.28's libstdc++ lacks statically,
  and its glibc is 2.28. So the floor comes from the toolchain, and
  nothing rewrites a symbol afterwards.

  The image runs in the build sandbox under bwrap, with the store
  mounted read-only beside it. nixpkgs pins every source, and the
  patched Nix is the one the rest of huggorm binds.
*/
{
  pkgs,
  lib,
  # huggorm's patched Nix, from `patchNix`, and the collector it links.
  nix,
  boehmgc,
  # The emitter, which setup.py runs.
  huggorm-gen,
  # The pure Python layer, whose suite tests the wheel.
  huggorm,
}:
let
  # `libs` below has a `boehmgc` of its own: the image's build of this one.
  boehmgcNix = boehmgc;

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

  # skopeo reads `$XDG_RUNTIME_DIR/containers/auth.json`, and without the
  # variable it reads `/run/containers/<uid>`, which the sandbox refuses.
  image = (pkgs.dockerTools.pullImage images.${pkgs.stdenv.hostPlatform.system}).overrideAttrs {
    XDG_RUNTIME_DIR = "/build";
  };

  rootfs = pkgs.runCommand "manylinux_2_28-rootfs" { nativeBuildInputs = [ pkgs.python3 ]; } ''
    python3 ${./rootfs.py} ${image} $out
  '';

  # meson from its own source, under the image's Python: nixpkgs' meson
  # carries patches that change how it searches for boost and rpaths.
  python = "/opt/python/cp312-cp312/bin/python3";
  meson = pkgs.writeTextFile {
    name = "meson";
    executable = true;
    destination = "/bin/meson";
    text = ''
      #!/bin/sh
      exec ${python} ${pkgs.meson.src}/meson.py "$@"
    '';
  };

  closureOf = deps: lib.unique (lib.concatMap (d: d.passthru.closure) deps);

  /**
    Build `src` inside the image, into `$out`.

    `deps` are other builds of this file. Each one's prefix reaches the
    compiler, the linker, pkg-config and cmake. `script` runs under the
    image's bash, in the unpacked source.
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
      closure = closureOf deps;
      join = sep: f: lib.concatMapStringsSep sep f closure;
      env = {
        PATH = lib.concatStringsSep ":" [
          "/opt/rh/gcc-toolset-14/root/usr/bin"
          (lib.makeBinPath tools)
          "/usr/local/bin"
          "/usr/bin"
          "/bin"
          "${meson}/bin"
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
      self =
        pkgs.runCommand "manylinux-${pname}${lib.optionalString (version != "") "-${version}"}"
          {
            nativeBuildInputs = [
              pkgs.bubblewrap
              pkgs.unzip
            ];
            passthru = {
              inherit pname version;
              closure = closure ++ [ self ];
            };
          }
          ''
            mkdir -p "$out" unpack
            cd unpack
            unpackFile ${src}
            chmod -R u+w .
            src=$(echo "$PWD"/*)
            cd ..
            bwrap --ro-bind ${rootfs} / --dev /dev --proc /proc --tmpfs /tmp \
              --ro-bind /nix/store /nix/store --bind "$out" "$out" \
              --bind "$NIX_BUILD_TOP" "$NIX_BUILD_TOP" \
              --clearenv${setenv} \
              --setenv out "$out" --setenv SRC "$src" \
              --setenv NIX_BUILD_CORES "$NIX_BUILD_CORES" \
              /bin/bash ${runner}
          '';
    in
    self;

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
  libs = rec {
    # nixpkgs' second patch turns bzip2's Makefile into autotools.
    bzip2 = build {
      inherit (pkgs.bzip2)
        pname
        version
        src
        patches
        patchFlags
        ;
      script = "autoreconf -fi\n" + autotools "";
    };
    xz = build {
      inherit (pkgs.xz) pname version src;
      script = autotools "--disable-doc --disable-scripts --disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo --disable-lzma-links";
    };
    zstd = build {
      inherit (pkgs.zstd) pname version src;
      script = ''
        make -C lib -j"$NIX_BUILD_CORES" libzstd PREFIX="$out"
        make -C lib install-pc install-includes install-shared PREFIX="$out" LIBDIR="$out/lib"
      '';
    };
    brotli = build {
      inherit (pkgs.brotli) pname version src;
      script = cmake "-DBROTLI_BUILD_TOOLS=OFF";
    };
    libsodium = build {
      inherit (pkgs.libsodium) pname version src;
      script = "[ -x configure ] || ./autogen.sh -s\n" + autotools "";
    };
    sqlite = build {
      inherit (pkgs.sqlite) pname version src;
      script = autotools "--disable-tcl";
    };
    openssl = build {
      inherit (pkgs.openssl) pname version src;
      # Configure needs IPC::Cmd, which the image's perl lacks.
      tools = [ pkgs.perl ];
      script = ''
        ./Configure --prefix="$out" --libdir=lib shared no-tests no-docs no-apps
        make -j"$NIX_BUILD_CORES"
        make install_sw
      '';
    };
    nghttp2 = build {
      inherit (pkgs.nghttp2) pname version src;
      script = autotools "--enable-lib-only";
    };
    curl = build {
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
    };
    libarchive = build {
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
    };
    libgit2 = build {
      inherit (pkgs.libgit2) pname version src;
      deps = [
        openssl
      ];
      script = cmake ''
        -DUSE_SSH=OFF -DUSE_HTTPS=OpenSSL -DUSE_GSSAPI=OFF -DREGEX_BACKEND=builtin \
        -DUSE_HTTP_PARSER=builtin -DBUILD_TESTS=OFF -DBUILD_CLI=OFF
      '';
    };
    # The modules Nix's meson files name, and not the rest of boost.
    boost = build {
      inherit (pkgs.nixDependencies.boost) pname version src;
      script = ''
        ./bootstrap.sh --prefix="$out" --with-libraries=context,coroutine,iostreams,url,container,system,thread
        ./b2 -j"$NIX_BUILD_CORES" install link=shared variant=release threading=multi \
          runtime-link=shared -sNO_ZLIB=1 -sNO_BZIP2=1 -sNO_LZMA=1 -sNO_ZSTD=1 cxxflags=-fPIC
      '';
    };
    libblake3 = build {
      inherit (pkgs.libblake3) pname version src;
      script = "cd c\n" + cmake "-DBLAKE3_USE_TBB=OFF";
    };
    nlohmann_json = build {
      inherit (pkgs.nlohmann_json) pname version src;
      script = cmake "-DJSON_BuildTests=OFF";
    };
    toml11 = build {
      inherit (pkgs.toml11) pname version src;
      script = cmake "";
    };
    editline = build {
      inherit (pkgs.editline) pname version src;
      script = "./autogen.sh\n" + autotools "";
    };
    # The collector libexpr links in the rest of huggorm, with its flags
    # and its patches.
    boehmgc = build {
      inherit (boehmgcNix)
        pname
        version
        src
        patches
        ;
      script = "./autogen.sh\n" + autotools (lib.escapeShellArgs boehmgcNix.configureFlags);
    };
  };

  # Nix's libraries, each its own meson project as nixpkgs builds them,
  # from the source huggorm patches. No CLI, no tests. The features off
  # here are the ones the rest of huggorm's Nix has and this one lacks:
  # seccomp, libcpuid, lowdown and AWS authentication for S3.
  nixLibs =
    let
      components = [
        [
          "libutil"
          "-Dcpuid=disabled"
        ]
        [
          "libstore"
          "-Dseccomp-sandboxing=disabled -Ds3-aws-auth=disabled"
        ]
        [
          "libfetchers"
          ""
        ]
        [
          "libexpr"
          "-Dgc=enabled"
        ]
        [
          "libflake"
          ""
        ]
        [
          "libmain"
          ""
        ]
        [
          "libcmd"
          "-Dmarkdown=disabled -Dreadline-flavor=editline"
        ]
      ];
    in
    build {
      pname = "nix";
      inherit (nix) version;
      inherit (nix.libs.nix-util) src;
      deps = lib.attrValues libs;
      # The parser asks for `parse.error detailed`, bison 3.6; the image has 3.0.4.
      tools = [ pkgs.bison ];
      script = ''
        export PKG_CONFIG_PATH="$out/lib/pkgconfig:$PKG_CONFIG_PATH"
        export LD_LIBRARY_PATH="$out/lib:$LD_LIBRARY_PATH"
        export BOOST_ROOT=${libs.boost}
        # `preloadNSS` calls dlopen, which glibc keeps in libdl before 2.34
        # and Nix's meson files never name.
        export LDFLAGS="$LDFLAGS -ldl"
      ''
      + lib.concatMapStrings (
        c:
        let
          name = builtins.elemAt c 0;
        in
        ''
          meson setup _build/${name} src/${name} --prefix="$out" --libdir=lib \
            --buildtype=release ${builtins.elemAt c 1}
          meson compile -C _build/${name}
          meson install -C _build/${name}
        ''
      ) components;
    };

  /**
    The extension, built by the package's own setup.py under the image's
    CPython, then repaired: auditwheel copies every library it links
    into the wheel under a hashed name, and refuses the tag if any
    object needs more than glibc 2.28.

    The generator needs Python 3.14, so the wheel is cp314. The pure
    Python packages setup.py imports come from nixpkgs' 3.14.
  */
  wheel =
    let
      sitePackages = lib.concatMapStringsSep ":" (p: "${p}/${pkgs.python3.sitePackages}") (
        pkgs.python3.pkgs.requiredPythonModules [
          pkgs.python3.pkgs.setuptools
          pkgs.python3.pkgs.nanobind
          huggorm-gen
        ]
      );
    in
    build {
      pname = "huggorm-bindings-wheel";
      inherit (nix) version;
      src = ../../packages/huggorm-bindings;
      deps = [ nixLibs ];
      script = ''
        export PYTHONPATH=${sitePackages}
        export HUGGORM_NIX_VERSION=${lib.escapeShellArg nix.libs.nix-store.version}
        mkdir -p huggorm_bindings
        cp ${nix.libs.nix-store.src}/src/nix/get-env.sh huggorm_bindings/get-env.sh
        /opt/python/cp314-cp314/bin/python3 -m pip wheel --no-build-isolation \
          --no-deps --no-index --wheel-dir dist .
        auditwheel repair --plat manylinux_2_28_x86_64 --wheel-dir "$out" dist/*.whl
      '';
    };

  /**
    huggorm's hermetic suite against the wheel, inside the image.

    The wheel is the only compiled code under test: pip installs it into
    the image's own CPython, and the pure Python around it (huggorm, its
    generated surface, pytest) comes from nixpkgs with nixpkgs' bindings
    filtered out. A nixpkgs C extension built against glibc 2.42 would
    fail to load here, so protobuf and multidict take their pure Python
    implementations.
  */
  check =
    let
      pure = lib.filter (p: (p.pname or "") != "huggorm-bindings") (
        pkgs.python3.pkgs.requiredPythonModules (
          [ huggorm ]
          ++ (with pkgs.python3.pkgs; [
            pytest
            anyio
            pytest-timeout
            huggorm-gen
          ])
        )
      );
    in
    build {
      pname = "huggorm-wheel-check";
      inherit (nix) version;
      src = ../../packages/huggorm;
      script = ''
        /opt/python/cp314-cp314/bin/python3 -m pip install --no-index --no-deps \
          --target /tmp/site ${wheel}/*.whl
        export PYTHONPATH=/tmp/site:${
          lib.concatMapStringsSep ":" (p: "${p}/${pkgs.python3.sitePackages}") pure
        }
        export PATH="$PATH:${pkgs.grpcurl}/bin"
        export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=python MULTIDICT_NO_EXTENSIONS=1
        export HUGGORM_NIX_VERSION=${lib.escapeShellArg nix.libs.nix-store.version} HUGGORM_NIX_GC=1
        /opt/python/cp314-cp314/bin/python3 -c \
          'import huggorm_bindings.eval as e; print("bindings from", e.__file__)'
        /opt/python/cp314-cp314/bin/python3 -m pytest -p no:cacheprovider -m "not live" tests \
          2>&1 | tee "$out/pytest.log"
      '';
    };
in
{
  inherit
    image
    rootfs
    build
    libs
    nixLibs
    wheel
    check
    ;
}
