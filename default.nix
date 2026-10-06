{
  # Where every dependency lives. nix/sources.nix says how it finds them:
  # the umbrella's lock, so huggorm builds the Nix nanopynix builds.
  sources ? import ./nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # The LANGUAGE a declaration is written in, and the reader that
  # parses one. No declaration and no emitter is in here, which is
  # what lets the two below depend on it without depending on each
  # other.
  huggorm-dsl = pkgs.python3Packages.buildPythonPackage {
    pname = "huggorm-dsl";
    version = "0.1.0";
    pyproject = true;
    src = ./packages/huggorm-dsl;
    build-system = [ pkgs.python3Packages.setuptools ];
    pythonImportsCheck = [ "huggorm_dsl" ];
  };
  # The DECLARATIONS: one document per Nix class, and the lists
  # saying which of them owns a compiled module. Data, plus the
  # statement of what data there is.
  huggorm-decl = pkgs.python3Packages.buildPythonPackage {
    pname = "huggorm-decl";
    version = "0.1.0";
    pyproject = true;
    src = ./packages/huggorm-decl;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ huggorm-dsl ];
    pythonImportsCheck = [ "huggorm_decl" ];
  };
  # The EMITTERS, both backends in one package. `cppgen` fills
  # huggorm_bindings, `pygen` fills huggorm_generated, and `payload`
  # is the hand-written Python pygen copies into its output.
  #
  # One package because they read ONE IR from one reader. A boundary
  # between them would say the split is architectural, and it is not.
  huggorm-gen = pkgs.python3Packages.buildPythonPackage {
    pname = "huggorm-gen";
    version = "0.1.0";
    pyproject = true;
    src = ./packages/huggorm-gen;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [
      huggorm-dsl
      huggorm-decl
    ];
    pythonImportsCheck = [ "huggorm_gen.cppgen" ];
  };
  versions = import ./nix/versions.nix { inherit pkgs; };

  # The variants a lane can instrument its Nix and the bindings with.
  # `nix/sanitizer.nix` gives each one's rules.
  sanitizers = {
    tsan = pkgs.callPackage ./nix/sanitizer.nix { name = "thread"; };
    ubsan = pkgs.callPackage ./nix/sanitizer.nix { name = "undefined"; };
    asan = pkgs.callPackage ./nix/sanitizer.nix { name = "address"; };
  };

  /**
    One Nix, patched, and everything huggorm builds against it, as a
    scope. `overrideScope` replaces a member for every member that takes
    it.

    # Inputs

    `components`
    : A nixpkgs Nix component scope, such as `pkgs.nixVersions.nixComponents_2_34`

    `patches`
    : The patches to append to every component

    `gc`
    : False builds libexpr with `-Dgc=disabled`. huggorm then makes no
      collector call, and the lane has no `boehmgc`.

    `sanitizer`
    : One of `sanitizers`, or null. It instruments every Nix component,
      the libraries `nix/sanitizer.nix` names, and the bindings.

    `wheels`
    : False when PyPA's manylinux image cannot build this Nix. A lane has
      `manylinux` wheels only with this, the collector and no sanitizer.
  */
  mkLane =
    {
      components,
      patches,
      gc ? true,
      sanitizer ? null,
      wheels ? true,
    }:
    assert lib.assertMsg (!(sanitizer.requiresNoGC or false) || !gc) ''
      The ${sanitizer.name} sanitizer needs `gc = false`: libexpr's meson
      refuses the collector with it, after a rebuild of the whole closure.
    '';
    let
      # One boost for every component that takes it: the ASAN boost
      # changes the layout of a type they share.
      ucontextBoost = lib.optionalAttrs (sanitizer.needsUcontextBoost or false) {
        boost = sanitizer.sanitizeBoost pkgs.boost;
      };
    in
    lib.makeScope pkgs.newScope (
      self:
      {
        inherit
          gc
          sanitizer
          huggorm-dsl
          huggorm-decl
          huggorm-gen
          ;
        inherit (pkgs) python3Packages;

        nixComponents =
          let
            patched = (components.appendPatches patches).overrideScope (
              _: prev:
              {
                nix-expr = prev.nix-expr.override (
                  (if gc then { inherit (self) boehmgc; } else { enableGC = false; }) // ucontextBoost
                );
              }
              // lib.optionalAttrs (sanitizer != null) {
                nix-util = prev.nix-util.override ucontextBoost;
                nix-store = prev.nix-store.override (
                  { sqlite = sanitizer.sanitizeSqlite pkgs.sqlite; } // ucontextBoost
                );
              }
            );
          in
          if sanitizer == null then
            patched
          else
            patched.overrideAllMesonComponents sanitizer.mesonComponentOverrides;
        inherit (self.nixComponents) version;
        # The CLI. nixpkgs' `nix_2_34` is `nix-everything`, which also
        # builds the manual and runs Nix's functional tests.
        nix = self.nixComponents.nix-cli;
        # The collector libexpr links. The bindings link the same one,
        # because a process loads one `libgc.so.1`.
        boehmgc =
          let
            patched = pkgs.nixDependencies.boehmgc.overrideAttrs (old: {
              patches = (old.patches or [ ]) ++ versions.boehmgcPatches;
            });
          in
          if !gc then
            null
          else if sanitizer == null then
            patched
          else
            sanitizer.sanitizeBoehmGC patched;

        # `get-env.sh` as the `nix` binary embeds it, which is the text
        # `nix develop` adds to the store. Nix's `generate-header` wraps
        # the file in a raw string that starts on the line after
        # `R"__NIX_STR(`, so the text gains a leading newline, and the
        # file alone hashes to another shell derivation.
        getEnvSh = pkgs.runCommand "get-env.sh" { } ''
          { echo; cat ${self.nixComponents.nix-store.src}/src/nix/get-env.sh; } > "$out"
        '';

        # The emitted C++ for this Nix. `HUGGORM_NIX_VERSION` picks each
        # declaration's `NIX_VERSION` branch (huggorm#55).
        bindings-src = bindings-src.overrideAttrs { HUGGORM_NIX_VERSION = self.version; };
        huggorm-bindings =
          let
            plain = self.callPackage ./packages/huggorm-bindings {
              inherit (self.nixComponents)
                nix-util
                nix-store
                nix-expr
                nix-fetchers
                nix-flake
                nix-cmd
                ;
            };
          in
          if sanitizer == null then
            plain
          else
            # The import check dlopens the extension into a plain CPython,
            # and a late dlopen cannot grow the static TLS block, so the
            # runtime is preloaded.
            plain.overrideAttrs (old: {
              env =
                (old.env or { })
                // {
                  NIX_CFLAGS_COMPILE = sanitizer.flags;
                  NIX_CFLAGS_LINK = sanitizer.linkFlag;
                }
                // sanitizer.buildEnv
                // lib.optionalAttrs (sanitizer.runtime != null) { LD_PRELOAD = sanitizer.runtime; };
              dontStrip = true;
            });
        # The same build under clang, with every warning an error. gcc's
        # -Wall misses what clang warns about (-Wbraced-scalar-init, 30
        # of them once), and clang is what a darwin lane and clangd
        # compile the emitted C++ with.
        huggorm-bindings-clang = self.huggorm-bindings.overrideAttrs (old: {
          name = "${old.name}-clang";
          nativeBuildInputs = old.nativeBuildInputs ++ [ pkgs.clang ];
          preBuild = (old.preBuild or "") + ''
            export CC=clang CXX=clang++ LDSHARED="clang++ -shared"
          '';
          env = (old.env or { }) // {
            NIX_CFLAGS_COMPILE = "-Werror";
          };
        });
        huggorm-generated = self.callPackage ./packages/huggorm-generated { };
        nix-tcp-store = self.callPackage ./packages/nix-tcp-store {
          inherit (self.nixComponents) nix-util nix-store;
        };
        nix-tcp-store-check = self.callPackage ./packages/nix-tcp-store/check.nix { };
        # HUGGORM_SKIP_SUITE=1 drops the in-build suite. `test` and
        # `check` need this package for the front door only, so a red
        # suite would otherwise block the loop that debugs it.
        huggorm =
          let
            plain = self.callPackage ./packages/huggorm { };
          in
          if builtins.getEnv "HUGGORM_SKIP_SUITE" == "1" then
            plain.overridePythonAttrs { doCheck = false; }
          else
            plain;
      }
      # nix build --file . lanes.nix_2_35.manylinux.checks
      // lib.optionalAttrs (wheels && gc && sanitizer == null) {
        manylinux = self.callPackage ./nix/manylinux {
          vivarium = import (sources.vivarium + "/lib.nix") { inherit pkgs; };
        };
      }
    );

  # Every lane, by the name nanopynix's CI uses. nix_2_34 is the default
  # below. ASAN needs `gc = false`, so `-asan` is also a lane with no
  # collector.
  lanes = {
    nix_2_34 = mkLane versions.nix_2_34;
    nix_2_34-nogc = mkLane (versions.nix_2_34 // { gc = false; });
    nix_2_34-tsan = mkLane (versions.nix_2_34 // { sanitizer = sanitizers.tsan; });
    nix_2_34-ubsan = mkLane (versions.nix_2_34 // { sanitizer = sanitizers.ubsan; });
    nix_2_34-asan = mkLane (
      versions.nix_2_34
      // {
        sanitizer = sanitizers.asan;
        gc = false;
      }
    );

    nix_2_35 = mkLane versions.nix_2_35;
    nix_2_35-nogc = mkLane (versions.nix_2_35 // { gc = false; });
    nix_2_35-tsan = mkLane (versions.nix_2_35 // { sanitizer = sanitizers.tsan; });
    nix_2_35-ubsan = mkLane (versions.nix_2_35 // { sanitizer = sanitizers.ubsan; });
    nix_2_35-asan = mkLane (
      versions.nix_2_35
      // {
        sanitizer = sanitizers.asan;
        gc = false;
      }
    );

    git = mkLane versions.git;
    git-nogc = mkLane (versions.git // { gc = false; });
    git-tsan = mkLane (versions.git // { sanitizer = sanitizers.tsan; });
    git-ubsan = mkLane (versions.git // { sanitizer = sanitizers.ubsan; });
    git-asan = mkLane (
      versions.git
      // {
        sanitizer = sanitizers.asan;
        gc = false;
      }
    );
  };
  inherit (lanes.nix_2_34)
    nix
    boehmgc
    huggorm-bindings
    huggorm-bindings-clang
    huggorm
    huggorm-generated
    manylinux
    nix-tcp-store
    nix-tcp-store-check
    ;

  # nix build --file . every-nix-src
  #
  # The emitted C++ for every Nix version, about 4 s each with no
  # compile. Each version picks its own declaration branches, so a
  # refusal that only one version reaches fails here (huggorm#129 sat
  # on main because nothing built the 2.35 one).
  every-nix-src = pkgs.linkFarm "huggorm-every-nix-src" (
    lib.mapAttrsToList (name: _: {
      inherit name;
      path = lanes.${name}.bindings-src;
    }) (lib.filterAttrs (_: v: v ? components) versions)
  );

  # nix run --file . python -- $args
  # to be able to run Python commands
  python = pkgs.python3;
  # nix run --file . python -- $args
  # to be able to run Python commands with our packages loaded
  ourPython = pkgs.python3.withPackages (
    ps: with ps; [
      huggorm-bindings
      huggorm-generated
      huggorm
      # The generator imports them, so the interpreter every check
      # runs against has to have them.
      huggorm-gen
      huggorm-decl
      huggorm-dsl
      # The suites run under pytest, in the devshell and in the build
      # alike. pytest-timeout because a hung test is the failure this
      # suite is most exposed to - a server that never came up, or a
      # native crash that took a thread with it.
      pytest
      anyio
      pytest-timeout
      # The inotify change source. A propagated dependency of
      # huggorm, so the interpreter check and test run against needs
      # it too.
      asyncinotify
      # For `check`, which types the two `setup.py` files too.
      types-setuptools
    ]
  );
  # The emitted front door, into the working tree.
  #
  # `huggorm/__init__.py` is generated (huggorm#64) and the tree keeps
  # none of it, so `packages/huggorm/huggorm/` is a NAMESPACE portion.
  # Python's finder - and zuban's - prefer a regular package found
  # LATER on the path, so without this the tree's `huggorm` is not the
  # one anything reads: the dev loop `nix run test` exists for is
  # gone, and `check` typechecks the store's copy while reporting the
  # tree's file names. Both were measured.
  #
  # It writes a gitignored file into the tree, which is what both
  # `setup.py` files already do with their own output.
  #
  # -P, or it copies the file onto itself. Without it the cwd goes
  # first on sys.path, so once the copy exists `import huggorm` finds
  # THAT one and source and destination are one file.
  #
  # Run from the repository root.
  frontDoor = ''
    ${ourPython}/bin/python3 -P -c 'import huggorm, shutil; shutil.copyfile(huggorm.__file__, "packages/huggorm/huggorm/__init__.py")'
  '';

  # nix run --file . check
  #
  # The same two tools the builds gate on, over the whole tree at once,
  # without a rebuild. zuban needs an interpreter rather than a search
  # path: a PEP 561 <pkg>-stubs package is found only through one, and
  # without it every binding type reads as Any and the check passes
  # while proving nothing (huggorm#27).
  check = pkgs.writeShellApplication {
    name = "check";
    runtimeInputs = [
      pkgs.ruff
      pkgs.zuban
      ourPython
    ];
    text = ''
      cd "''${1:-.}"
      ${frontDoor}
      echo "--- lint ---"
      ruff check --no-cache packages examples
      # `mypy.ini` names every tree, so an editor and this read one
      # configuration. The two setup scripts are both module `setup`,
      # and the emitted package lives in the store, so each is its
      # own run under the same configuration.
      echo "--- typecheck: every tree mypy.ini names ---"
      zuban mypy --python-executable "${ourPython}/bin/python3"
      # The declarations branch on `NIX_2_35` and `NIX_2_36`, and
      # `mypy.ini` holds both false, so each later Nix gets a run of
      # its own. The suite and the generated surface need no such run
      # here: each version's own build typechecks them (huggorm#55).
      echo "--- typecheck: the declarations, for 2.35 and 2.36 ---"
      zuban mypy --python-executable "${ourPython}/bin/python3" \
        --always-true NIX_2_35 --always-false NIX_2_36 \
        packages/huggorm-dsl/src/huggorm_dsl packages/huggorm-decl/src/huggorm_decl
      zuban mypy --python-executable "${ourPython}/bin/python3" \
        --always-true NIX_2_35 --always-true NIX_2_36 \
        packages/huggorm-dsl/src/huggorm_dsl packages/huggorm-decl/src/huggorm_decl
      echo "--- typecheck: the setup scripts ---"
      zuban mypy --python-executable "${ourPython}/bin/python3" packages/huggorm-bindings/setup.py
      zuban mypy --python-executable "${ourPython}/bin/python3" packages/huggorm-generated/setup.py
      echo "--- typecheck: the emitted package ---"
      zuban mypy --python-executable "${ourPython}/bin/python3" \
        "${huggorm-generated}/lib/python3.14/site-packages/huggorm_generated"
      # Built before this script runs, so a clang warning fails `check`.
      echo "--- clang -Werror: ${huggorm-bindings-clang} ---"
      echo "--- tcp:// over loopback: ${nix-tcp-store-check} ---"
      echo "--- emitted C++ for every Nix: ${every-nix-src} ---"
      echo "all checks passed"
    '';
  };

  # nix run --file . test -- [pytest args]
  #
  # The WHOLE suite, outside the sandbox. A build has no daemon, no db
  # and no writable store, so anything that touches a real store cannot
  # be a build check - and that half will only grow (huggorm#37). The
  # build runs `pytest -m "not live"`; this runs everything.
  #
  # PYTHONPATH puts the working tree's huggorm ahead of the installed
  # copy, so an edit is testable without a rebuild. huggorm_bindings
  # and huggorm_generated still come from the store: one is compiled
  # and the other is generated, so neither exists in the tree.
  #
  # `frontDoor` first, or the tree's huggorm is a namespace portion
  # and the store's package wins the whole directory. See its comment.
  #
  # PYTEST_DEBUG_TEMPROOT gives this suite its OWN basedir, and that
  # is huggorm#62. pytest's default is `$TMPDIR/pytest-of-$USER`,
  # keyed by USER and not by project, so every pytest on this machine
  # shares one directory. Three consequences, all measured:
  #
  #   - another project's leftovers are counted as this suite's, and
  #     were, twice;
  #   - its unremovable directories warn on every run here - 177 of
  #     them once, enough to push the totals line off the screen,
  #     which is how a broken build read as `0 FAILED`;
  #   - reclaiming means deleting everybody's.
  #
  # A FIXED path, not a per-run one. pytest keeps the three newest
  # numbered directories under the basedir and prunes the rest, so a
  # fresh root per run would defeat the pruning it relies on.
  #
  # The variable is named for debugging and is documented surface -
  # `pytest --help` lists it - and it only replaces the PARENT. The
  # `pytest-of-$USER` directory, the numbering, the locks and the
  # retention all still work exactly as they did.
  test = pkgs.writeShellApplication {
    name = "test";
    runtimeInputs = [
      pkgs.gitMinimal
      ourPython
    ];
    text = ''
      cd "''${HUGGORM_ROOT:-.}"
      ${frontDoor}
      cd packages/huggorm
      export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
      # The oracle the devshell's live test runs: the CLI of the Nix the
      # bindings link, and a `bash` for its derivation's builder.
      export HUGGORM_ORACLE_NIX="${nix}/bin/nix"
      export HUGGORM_ORACLE_BASH="${pkgs.bash}"
      export HUGGORM_TCP_STORE_PLUGINS="${nix-tcp-store}/lib/nix/plugins"
      export PYTEST_DEBUG_TEMPROOT="''${PYTEST_DEBUG_TEMPROOT:-/tmp/huggorm}"
      mkdir -p "$PYTEST_DEBUG_TEMPROOT"
      # SAID OUT LOUD, because no gate can hold this. The suite runs
      # inside a build sandbox too, where the variable is unset and
      # the default root is right - so a test asserting the root
      # would have to skip there, and a gate that skips when it
      # breaks is this repo's named failure mode. One line names the
      # root instead, and a run that lost it says so.
      echo "temp: $PYTEST_DEBUG_TEMPROOT/pytest-of-$(id -un)"
      exec pytest "$@"
    '';
  };

  # nix run --file . show -- [files|surface]
  #
  # Read what the build produced, without knowing where the store put
  # it: the package sits at a store path nobody types. `surface` prints
  # the generated modules and `_policy.py`, the tables the wire reads.
  show = pkgs.writeShellApplication {
    name = "show";
    runtimeInputs = [
      ourPython
    ];
    text = ''
            pkg="${huggorm-generated}/lib/python3.14/site-packages"
            gen="$pkg/huggorm_generated"
            case "''${1:-files}" in
              files)
                echo "generated package: $gen"
                ls -1 "$gen"
                echo
                echo "binding stubs: $pkg/huggorm_bindings-stubs"
                ls -1 "$pkg/huggorm_bindings-stubs"
                ;;
              surface)
                for f in "$gen"/async_*.py "$gen"/protocols.py "$gen"/rpc.py \
                         "$gen"/free_functions.py "$gen"/_policy.py; do
                  echo "=== $f ==="
                  cat "$f"
                done
                ;;
              *)
                echo "usage: show [files|surface]" >&2
                exit 2
                ;;
            esac
    '';
  };

  # nix build --file . bindings-src
  #
  # The emitted C++, on its own, for reading.
  #
  # There is no other way to see it. The bindings leaf writes its
  # sources at setup.py import and compiles them in the same build, so
  # nothing in the tree and nothing installed holds a `.cpp`. A
  # reviewer of a codegen change wants exactly that file.
  #
  # It runs the same emitter the leaf runs, with the same argument, so
  # this cannot show something the build did not produce.
  bindings-src =
    pkgs.runCommand "huggorm-bindings-src"
      {
        nativeBuildInputs = [
          (pkgs.python3.withPackages (_: [
            huggorm-gen
            huggorm-decl
            huggorm-dsl
          ]))
        ];
      }
      ''
        mkdir -p "$out"
        python3 -m huggorm_gen.cppgen.generate "$out"
      '';

  shell = pkgs.mkShell {
    packages = [
      ourPython
      pkgs.zuban
    ];
    shellHook = # bash
      ''
        export PYBIN="${lib.getExe ourPython}"
      '';
  };
}
