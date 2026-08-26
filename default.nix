{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # fake-library should be a C++ project with "complex types", it doesn't have to do anything useful
  fake-library = pkgs.callPackage ./fake-library { };
  # The declarations, and the emitters that read them.
  #
  # An ordinary Python distribution, and stdlib-only: it parses
  # declarations with `ast` and writes text. Two builds use it -
  # cythonix-bindings for its source, cythonix-generated for its
  # manifest entries - which is why it is a package rather than a
  # directory each of them reaches into.
  cythonix-idl = pkgs.python3Packages.buildPythonPackage {
    pname = "cythonix-idl";
    version = "0.1.0";
    pyproject = true;
    src = ./cythonix-idl;
    build-system = [ pkgs.python3Packages.setuptools ];
    pythonImportsCheck = [ "cythonix_idl" ];
  };
  # The interpreter the emitters run under, with them on its path.
  idlPython = pkgs.python3.withPackages (_: [ cythonix-idl ]);
  # The SAME declarations, through the other backend.
  #
  # `emit.py` writes Cython from the declarations; `nbemit.py` writes
  # C++ from the same files. Until this existed the second one had
  # only ever been checked as TEXT, which proves it could have written
  # a binding and not that the binding works. This compiles it against
  # real Nix, and `parity.py` then asks both backends the same
  # questions.
  #
  # A PACKAGE, not one module. Two things follow from that, and both
  # are the point. A declaration file owns a module, so `decl/path.py`
  # and `decl/store.py` compile to `path` and `store` the way they do
  # under Cython. And `store` returns a `nix::StorePath`, which is a
  # class `path` registered - nanobind keeps one type registry per
  # process, so the two modules share it as long as `store` imports
  # `path` first. `nbemit.imports` derives that import from the
  # declaration's own `from ... import StorePath`.
  #
  # The name is `cythonix_nb` rather than `cythonix_bindings` only
  # because `parity.py` imports BOTH backends into one interpreter.
  # They swap names when Cython goes.
  #
  # nanobind ships its runtime as source rather than a library, so
  # each extension compiles `nb_combined.cpp` beside its own
  # translation unit. `ext/robin_map` is nanobind's vendored hash map,
  # which its own headers include and its wheel does not put on the
  # include path.
  nb-bindings = pkgs.stdenv.mkDerivation {
    name = "cythonix-nb";
    # No sources of its own: the declarations and the emitter both
    # come from the cythonix-idl package on the interpreter's path,
    # and the files this compiles are written in the build phase.
    dontUnpack = true;
    nativeBuildInputs = [ pkgs.pkg-config idlPython ];
    buildInputs = [ idlPython pkgs.python3.pkgs.nanobind ] ++ (with pkgs.nix.libs; [
      nix-util
      nix-store
    ]);
    # One entry per declaration that owns a module. The same list
    # `generate.py` keeps for Cython, and for the same reason: which
    # declarations the build compiles is the build's decision.
    modules = [ "path" "store" ];
    buildPhase = ''
      inc=$(python3 -c 'import nanobind; print(nanobind.include_dir())')
      src=$(python3 -c 'import nanobind; print(nanobind.source_dir())')
      pyinc=$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["include"])')
      ext=$(python3 -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')
      mkdir -p cythonix_nb
      touch cythonix_nb/__init__.py
      for mod in $modules; do
        python3 -m cythonix_idl.generate_nb "decl/$mod.py" \
          "cythonix_nb.$mod" "$mod.cpp"
        # -I on the bindings directory for _cpp/errors.hpp, which is
        # C++ that neither backend owns: it maps a nix exception onto
        # the right class in cythonix_bindings.errors. Cython reaches
        # it through `except +translate_nix_error`; nanobind through
        # one registered translator.
        $CXX -std=c++23 -O1 -fPIC -shared -fvisibility=hidden \
          -I"$inc" -I"$pyinc" -I"$inc/../ext/robin_map/include" \
          -I"${./cythonix-bindings}" \
          $(pkg-config --cflags nix-store) \
          "$mod.cpp" "$src/nb_combined.cpp" \
          $(pkg-config --libs nix-store) \
          -o "cythonix_nb/$mod$ext"
      done
    '';
    installPhase = ''
      mkdir -p $out
      cp -r cythonix_nb $out/
      cp ./*.cpp $out/
    '';
  };
  # The binding source that actually gets compiled.
  #
  # This is the step that makes the declaration load-bearing. Before
  # it, the emitter wrote its files beside the hand-written ones and a
  # gate diffed them - which proves the emitter COULD have written the
  # binding. Here it DOES: `path.pyx`, `path.pxd` and `c_path.pxd` are
  # not in the repo at all, and the only thing standing behind
  # `cythonix_bindings.path` is `cythonix-idl/src/cythonix_idl/decl/path.py`.
  bindings-src = pkgs.runCommand "cythonix-bindings-src" { } ''
    cp -r ${./cythonix-bindings} $out
    chmod -R u+w $out
    ${lib.getExe idlPython} -m cythonix_idl.generate $out/cythonix_bindings
  '';
  # this is Cython bindings into fake-library, should contain pxd and pyx (I believe)
  cythonix-bindings = pkgs.callPackage ./cythonix-bindings {
    inherit fake-library;
    src = bindings-src;
  };
  # this is a Python library that uses cythonix-bindings
  cythonix = pkgs.callPackage ./cythonix {
    inherit fake-library;
    inherit cythonix-bindings;
    inherit cythonix-generated;
  };
  # AST codegen layer between bindings and python: pxd + live bindings -> generated stubs
  cythonix-generated = pkgs.callPackage ./cythonix-generated {
    inherit fake-library;
    inherit cythonix-bindings;
    inherit cythonix-idl;
  };
  # nix run --file . python -- $args
  # to be able to run Python commands
  python = pkgs.python3.withPackages (ps: with ps; [ cython ]);
  # nix run --file . python -- $args
  # to be able to run Python commands with our packages loaded
  ourPython = pkgs.python3.withPackages (
    ps: with ps; [
      cython
      cythonix-bindings
      cythonix-generated
      cythonix
      # The generator imports it, so the interpreter every check runs
      # against has to have it.
      cythonix-idl
      # The suites run under pytest, in the devshell and in the build
      # alike. pytest-timeout because a hung test is the failure this
      # suite is most exposed to - a server that never came up, or a
      # native crash that took a thread with it.
      pytest
      anyio
      pytest-timeout
    ]
  );
  # nix run --file . check
  #
  # The same two tools the builds gate on, over the whole tree at once,
  # without a rebuild. zuban needs an interpreter rather than a search
  # path: a PEP 561 <pkg>-stubs package is found only through one, and
  # without it every binding type reads as Any and the check passes
  # while proving nothing (tasks/027).
  check = pkgs.writeShellApplication {
    name = "check";
    runtimeInputs = [
      pkgs.ruff
      pkgs.zuban
      ourPython
      spike
    ];
    text = ''
      cd "''${1:-.}"
      echo "--- lint ---"
      ruff check --no-cache cythonix cythonix-generated cythonix-bindings \
        cythonix-idl
      echo "--- typecheck: the generator ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        --exclude 'smoke_test\.py$' \
        cythonix-generated/generator/src/codegen
      echo "--- typecheck: the hand-written layer and the suites ---"
      ( cd cythonix \
        && zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
             cythonix tests )
      echo "--- typecheck: the emitted package ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        "${cythonix-generated}/lib/python3.14/site-packages/cythonix_generated"
      echo "--- the declarations' gates ---"
      spike
      echo "all checks passed"
    '';
  };

  # nix run --file . spike
  #
  # The declarations' own gates: emit from a declaration and diff
  # against something built the other way.
  #
  # Two references, and only one of them is in this repo. The Cython
  # half compares against cythonix-bindings and the manifest a real
  # build produced, so it is hermetic and always runs. The nanobind
  # half compares against ~/Code/nanopynix, which is hand-written,
  # tested and NOT here - so it is skipped with a reason rather than
  # failing on a machine that does not have it.
  spike = pkgs.writeShellApplication {
    name = "spike";
    runtimeInputs = [ ourPython ];
    text = ''
      cd "''${1:-.}/cythonix-idl/gates"
      manifest="${cythonix-generated}/lib/python3.14/site-packages/cythonix_generated/manifest.json"
      echo "--- declaration -> Cython, and -> manifest ---"
      python3 check.py --manifest "$manifest"
      echo "--- one declaration, two compiled backends ---"
      python3 parity.py "${nb-bindings}"
      if [ -d "$HOME/Code/nanopynix" ]; then
        echo "--- declaration -> nanobind ---"
        python3 nbcheck.py
      else
        echo "--- declaration -> nanobind: SKIPPED, no ~/Code/nanopynix ---"
      fi
    '';
  };

  # nix run --file . test -- [pytest args]
  #
  # The WHOLE suite, outside the sandbox. A build has no daemon, no db
  # and no writable store, so anything that touches a real store cannot
  # be a build check - and that half will only grow (tasks/037). The
  # build runs `pytest -m "not live"`; this runs everything.
  #
  # PYTHONPATH puts the working tree's cythonix ahead of the installed
  # copy, so an edit is testable without a rebuild. cythonix_bindings
  # and cythonix_generated still come from the store: one is compiled
  # and the other is generated, so neither exists in the tree.
  test = pkgs.writeShellApplication {
    name = "test";
    runtimeInputs = [
      pkgs.grpcurl
      ourPython
    ];
    text = ''
      cd "''${CYTHONIX_ROOT:-.}/cythonix"
      export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
      exec pytest "$@"
    '';
  };

  # nix run --file . show -- [files|manifest|proto|surface]
  #
  # Read what the build produced, without knowing where the store put
  # it. Four stages come out of one generator run and only one of them
  # is a Python module a reader can open: the manifest is the contract
  # between them, and the schema is a binary FileDescriptorSet that no
  # editor renders.
  #
  # So this exists for a reader rather than for the build. `proto`
  # especially: the wire is generated from declarations next to the
  # bindings, and the only honest way to review it is to read what
  # actually got emitted.
  show = pkgs.writeShellApplication {
    name = "show";
    runtimeInputs = [
      pkgs.grpcurl
      pkgs.jq
      ourPython
    ];
    text = ''
      pkg="${cythonix-generated}/lib/python3.14/site-packages"
      gen="$pkg/cythonix_generated"
      case "''${1:-files}" in
        files)
          echo "generated package: $gen"
          ls -1 "$gen"
          echo
          echo "binding stubs: $pkg/cythonix_bindings-stubs"
          ls -1 "$pkg/cythonix_bindings-stubs"
          ;;
        manifest)
          jq . "$gen/manifest.json"
          ;;
        proto)
          # Every service and every message, as .proto text. The names
          # come from the descriptor set itself, so nothing here has a
          # list to keep in step.
          names=$(python3 -c "
      import sys
      from google.protobuf import descriptor_pb2
      fds = descriptor_pb2.FileDescriptorSet()
      fds.ParseFromString(open(sys.argv[1], 'rb').read())
      for f in fds.file:
          for m in f.message_type:
              print(f'{f.package}.{m.name}')
          for s in f.service:
              print(f'{f.package}.{s.name}')
      " "$gen/grpc_schema.pb")
          # One symbol per call: grpcurl describes one at a time.
          for name in $names; do
            grpcurl -protoset "$gen/grpc_schema.pb" describe "$name"
            echo
          done
          ;;
        surface)
          for f in "$gen"/async_*.py "$gen"/protocols.py "$gen"/rpc.py \
                   "$gen"/free_functions.py; do
            echo "=== $f ==="
            cat "$f"
          done
          ;;
        *)
          echo "usage: show [files|manifest|proto|surface]" >&2
          exit 2
          ;;
      esac
    '';
  };

  shell = pkgs.mkShell {
    packages = [
      ourPython
      pkgs.zuban
      pkgs.grpcurl
    ];
    shellHook = # bash
    ''
      export PYBIN="${lib.getExe ourPython}"
    '';
  };
}
