{
  pkgs ? import <nixpkgs> { },
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
  #
  # protobuf is pygen's alone and is NOT a dependency here - it is an
  # extra, so compiling the bindings does not drag it in. The build
  # that needs it declares it.
  huggorm-gen = pkgs.python3Packages.buildPythonPackage {
    pname = "huggorm-gen";
    version = "0.1.0";
    pyproject = true;
    src = ./packages/huggorm-gen;
    build-system = [ pkgs.python3Packages.setuptools ];
    dependencies = [ huggorm-dsl huggorm-decl ];
    pythonImportsCheck = [ "huggorm_gen.cppgen" ];
  };
  # The interpreter the emitters run under, with them on its path.
  # The bindings. Every module is a nanobind extension whose C++ its
  # own setup.py writes from a declaration, before setuptools is told
  # the sources exist.
  huggorm-bindings = pkgs.callPackage ./packages/huggorm-bindings {
    inherit huggorm-gen huggorm-decl huggorm-dsl;
  };
  # this is a Python library that uses huggorm-bindings
  huggorm = pkgs.callPackage ./packages/huggorm {
    inherit huggorm-bindings;
    inherit huggorm-generated;
  };
  # AST codegen layer between bindings and python: the declarations
  # -> async wrappers, protocols, an RPC client, a wire schema and
  # the binding stubs.
  huggorm-generated = pkgs.callPackage ./packages/huggorm-generated {
    inherit huggorm-bindings;
    inherit huggorm-gen huggorm-decl huggorm-dsl;
  };
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
      ruff check --no-cache packages
      echo "--- typecheck: the generator ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        --exclude 'smoke_test\.py$' \
        packages/huggorm-gen/src/huggorm_gen
      echo "--- typecheck: the hand-written layer and the suites ---"
      ( cd packages/huggorm \
        && zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
             huggorm tests )
      echo "--- typecheck: the emitted package ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        "${huggorm-generated}/lib/python3.14/site-packages/huggorm_generated"
      echo "--- the declarations' gates ---"
      spike
      echo "all checks passed"
    '';
  };

  # nix run --file . spike
  #
  # The declaration's one remaining text gate.
  #
  # There were three. Two compared an emitted file against something
  # built the other way: emitted Cython against the repo's own, and a
  # nanobind StorePath against a Cython one. Both are gone, because
  # what they compared against is gone - `huggorm_bindings` IS the
  # emitted nanobind now, and a diff of a file against itself proves
  # nothing.
  #
  # What replaced them is stronger than a text diff and it is already
  # in this file: the modules COMPILE from the declarations, they
  # import, and 158 tests drive them.
  #
  # This one is left because what it reads is not in this repo.
  # ~/Code/nanopynix is hand-written, tested nanobind over the same
  # library, so emitting against it says something the build cannot.
  #
  # A corpus, not a reference. It is hand-written and therefore
  # inconsistent, and the emitter is meant to beat it rather than
  # match it - so the gate classifies every difference instead of
  # demanding there be none. Skipped with a reason on a machine that
  # does not have it.
  spike = pkgs.writeShellApplication {
    name = "spike";
    runtimeInputs = [ ourPython ];
    text = ''
      cd "''${1:-.}/packages/huggorm-gen/gates"
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
  # PYTHONPATH puts the working tree's huggorm ahead of the installed
  # copy, so an edit is testable without a rebuild. huggorm_bindings
  # and huggorm_generated still come from the store: one is compiled
  # and the other is generated, so neither exists in the tree.
  test = pkgs.writeShellApplication {
    name = "test";
    runtimeInputs = [
      pkgs.grpcurl
      ourPython
    ];
    text = ''
      cd "''${HUGGORM_ROOT:-.}/packages/huggorm"
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
