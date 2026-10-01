/**
  Each library Nix links, built in the manylinux image from the source
  nixpkgs pins, with every optional feature off that Nix does not use.
  Then Nix's own libraries.

  `libraries` holds package functions, which the scope `callPackage`s:
  an argument naming another library gets the image's build of it.
  `nixLibs` takes every one of them as `deps`.

  zlib is the image's own: `libz.so.1` is on the manylinux policy's list
  of libraries a host provides, so auditwheel never bundles it.
*/
{
  pkgs,
  lib,
  # huggorm's patched Nix, and the collector it links.
  nix,
  boehmgc,
}:
{
  libraries = {
    # nixpkgs' second patch turns bzip2's Makefile into autotools.
    bzip2 =
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
      };

    xz =
      { build, autotools }:
      build {
        inherit (pkgs.xz) pname version src;
        script = autotools "--disable-doc --disable-scripts --disable-xz --disable-xzdec --disable-lzmadec --disable-lzmainfo --disable-lzma-links";
      };

    zstd =
      { build }:
      build {
        inherit (pkgs.zstd) pname version src;
        script = ''
          make -C lib -j"$NIX_BUILD_CORES" libzstd PREFIX="$out"
          make -C lib install-pc install-includes install-shared PREFIX="$out" LIBDIR="$out/lib"
        '';
      };

    brotli =
      { build, cmake }:
      build {
        inherit (pkgs.brotli) pname version src;
        script = cmake "-DBROTLI_BUILD_TOOLS=OFF";
      };

    libsodium =
      { build, autotools }:
      build {
        inherit (pkgs.libsodium) pname version src;
        script = "[ -x configure ] || ./autogen.sh -s\n" + autotools "";
      };

    sqlite =
      { build, autotools }:
      build {
        inherit (pkgs.sqlite) pname version src;
        script = autotools "--disable-tcl";
      };

    openssl =
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
      };

    nghttp2 =
      { build, autotools }:
      build {
        inherit (pkgs.nghttp2) pname version src;
        script = autotools "--enable-lib-only";
      };

    curl =
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
      };

    libarchive =
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
      };

    libgit2 =
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
      };

    # The modules Nix's meson files name, and not the rest of boost.
    boost =
      { build }:
      build {
        inherit (pkgs.nixDependencies.boost) pname version src;
        script = ''
          ./bootstrap.sh --prefix="$out" --with-libraries=context,coroutine,iostreams,url,container,system,thread
          ./b2 -j"$NIX_BUILD_CORES" install link=shared variant=release threading=multi \
            runtime-link=shared -sNO_ZLIB=1 -sNO_BZIP2=1 -sNO_LZMA=1 -sNO_ZSTD=1 cxxflags=-fPIC
        '';
      };

    libblake3 =
      { build, cmake }:
      build {
        inherit (pkgs.libblake3) pname version src;
        script = "cd c\n" + cmake "-DBLAKE3_USE_TBB=OFF";
      };

    nlohmann_json =
      { build, cmake }:
      build {
        inherit (pkgs.nlohmann_json) pname version src;
        script = cmake "-DJSON_BuildTests=OFF";
      };

    toml11 =
      { build, cmake }:
      build {
        inherit (pkgs.toml11) pname version src;
        script = cmake "";
      };

    editline =
      { build, autotools }:
      build {
        inherit (pkgs.editline) pname version src;
        script = "./autogen.sh\n" + autotools "";
      };

    # The collector libexpr links in the rest of huggorm, with its flags
    # and its patches.
    boehmgc =
      { build, autotools }:
      build {
        inherit (boehmgc)
          pname
          version
          src
          patches
          ;
        script = "./autogen.sh\n" + autotools (lib.escapeShellArgs boehmgc.configureFlags);
      };
  };

  /**
    Nix's libraries, each its own meson project as nixpkgs builds them,
    from the source huggorm patches, and linked against every library
    above. No CLI, no tests. The features off here are the ones the rest
    of huggorm's Nix has and this one lacks: seccomp, libcpuid, lowdown
    and AWS authentication for S3.
  */
  nixLibs =
    {
      build,
      boost,
      deps,
    }:
    let
      # In link order: each one links the ones before it.
      components = [
        {
          name = "libutil";
          flags = "-Dcpuid=disabled";
        }
        {
          name = "libstore";
          flags = "-Dseccomp-sandboxing=disabled -Ds3-aws-auth=disabled";
        }
        {
          name = "libfetchers";
          flags = "";
        }
        {
          name = "libexpr";
          flags = "-Dgc=enabled";
        }
        {
          name = "libflake";
          flags = "";
        }
        {
          name = "libmain";
          flags = "";
        }
        {
          name = "libcmd";
          flags = "-Dmarkdown=disabled -Dreadline-flavor=editline";
        }
      ];
    in
    build {
      pname = "nix";
      inherit (nix) version;
      inherit (nix.libs.nix-util) src;
      inherit deps;
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
      + lib.concatMapStrings (c: ''
        meson setup _build/${c.name} src/${c.name} --prefix="$out" --libdir=lib \
          --buildtype=release ${c.flags}
        meson compile -C _build/${c.name}
        meson install -C _build/${c.name}
      '') components;
    };
}
