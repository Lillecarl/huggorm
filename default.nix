{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # The declarations, and the emitters that read them.
  #
  # An ordinary Python distribution, and stdlib-only: it parses
  # declarations with `ast` and writes text. Two builds use it -
  # huggorm-bindings for its source, huggorm-generated for its
  # manifest entries - which is why it is a package rather than a
  # directory each of them reaches into.
  huggorm-idl = pkgs.python3Packages.buildPythonPackage {
    pname = "huggorm-idl";
    version = "0.1.0";
    pyproject = true;
    src = ./huggorm-idl;
    build-system = [ pkgs.python3Packages.setuptools ];
    pythonImportsCheck = [ "huggorm_idl" ];
  };
  # The interpreter the emitters run under, with them on its path.
  idlPython = pkgs.python3.withPackages (_: [ huggorm-idl ]);
  # The binding source that actually gets compiled.
  #
  # This is the step that makes the declaration load-bearing. Before
  # it, the emitter wrote its files beside the hand-written ones and a
  # gate diffed them - which proves the emitter COULD have written the
  # binding. Here it DOES: there is no binding source in the repo at
  # all, and the only thing standing behind `huggorm_bindings.path`
  # is `huggorm-idl/src/huggorm_idl/decl/path.py`.
  bindings-src = pkgs.runCommand "huggorm-bindings-src" { } ''
    cp -r ${./huggorm-bindings} $out
    chmod -R u+w $out
    ${lib.getExe idlPython} -m huggorm_idl.generate $out/huggorm_bindings
  '';
  # The bindings. Every module is a nanobind extension whose C++ is
  # written from a declaration before this builds.
  huggorm-bindings = pkgs.callPackage ./huggorm-bindings {
    inherit huggorm-idl;
    src = bindings-src;
  };
  # this is a Python library that uses huggorm-bindings
  huggorm = pkgs.callPackage ./huggorm {
    inherit huggorm-bindings;
    inherit huggorm-generated;
  };
  # AST codegen layer between bindings and python: the declarations
  # -> async wrappers, protocols, an RPC client, a wire schema and
  # the binding stubs.
  huggorm-generated = pkgs.callPackage ./huggorm-generated {
    inherit huggorm-bindings;
    inherit huggorm-idl;
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
      # The generator imports it, so the interpreter every check runs
      # against has to have it.
      huggorm-idl
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
      ruff check --no-cache huggorm huggorm-generated huggorm-bindings \
        huggorm-idl
      echo "--- typecheck: the generator ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        --exclude 'smoke_test\.py$' \
        huggorm-generated/generator/src/codegen
      echo "--- typecheck: the hand-written layer and the suites ---"
      ( cd huggorm \
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
      cd "''${1:-.}/huggorm-idl/gates"
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
      cd "''${HUGGORM_ROOT:-.}/huggorm"
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
