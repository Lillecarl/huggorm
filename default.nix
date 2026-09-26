{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # The Nix this repository BINDS, with one patch carried on it.
  #
  # `EvalState` allocates its base environment once, at a size
  # `src/libexpr/eval.cc` fixes as `BASE_ENV_SIZE = 128`, and neither
  # `addConstant` nor `addPrimOp` tests a bound before writing
  # `baseEnv.values[baseEnvDispl++]`. Nix 2.34 publishes 119 names
  # under `builtins`, so a stock evaluator has NINE slots left and the
  # tenth registered primop writes past the end of the block.
  #
  # `tasks/033` measured the headroom here; the patch header measures
  # the overflow, under AddressSanitizer, and explains why the size is
  # a constant at all. Two things in it are worth knowing before
  # reading the diff:
  #
  # - The collector HIDES this. `allocBytes` is GC_MALLOC and Boehm
  #   rounds up to a size class, so the write lands in the block's
  #   slack and nothing reports it. Only a build with no collector
  #   gets an exact allocation and aborts.
  # - There are TWO containers of 128. `createBaseEnv` also builds the
  #   `builtins` attribute set with `buildBindings(128)` and pushes
  #   into it through a `const_cast` that goes around the capacity
  #   assert. Raising only the base environment moves the corruption
  #   rather than removing it, so both go up together.
  #
  # Taken from ~/Code/nanopynix, which found it and carries the same
  # file. Carl's call, 2026-09-03: "there are tiny patches required to
  # make good bindings for now, eventually I'll work on upstreaming
  # dynamic env sizing".
  #
  # The second patch lets an interrupted thunk be forced again. Nix
  # caches every non-recoverable error in the thunk it came from, and
  # `nix::Interrupted` is one, so a cancelled call left every value
  # it was forcing rethrowing "interrupted by the user" for the life
  # of the state (tasks/097). Carl's call, 2026-09-25: patch Nix, not
  # abandon the state. It is upstream's own fix, 5c4f498d3, released
  # in 2.35.0, so it goes when `pkgs.nix` reaches 2.35.
  #
  # One version, and nanopynix's shape is deliberately
  # not copied. It keys a patch table by `majorMinor` and builds a
  # scope per version, because it supports 2.34 through git. This
  # repository binds the nix that `pkgs.nix` is, and Carl put it this
  # way on the same day: nanopynix is production ready, "this is still
  # an elaborate spike". A version matrix is a cost that buys nothing
  # until there is a second version to serve.
  #
  # The patch header says the three hunks have identical context in
  # 2.31, 2.34 and 2.35, so a version bump moves line numbers and
  # nothing else. If it ever stops applying, that failure is the
  # signal to read it again - not to add a matrix.
  #
  # It raises both sizes to 512 and makes the two base-environment
  # writes TEST the bound, so a consumer that still exceeds it reads
  # an error instead of corrupting the heap.
  nix = pkgs.nix.appendPatches [
    ./nix/patches/nix-base-env-size.patch
    ./nix/patches/nix-interrupted-thunk-recovers.patch
  ];
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
    inherit (nix.libs)
      nix-util
      nix-store
      nix-expr
      nix-fetchers
      nix-flake
      ;
  };
  # this is a Python library that uses huggorm-bindings
  huggorm = pkgs.callPackage ./packages/huggorm {
    inherit huggorm-bindings;
    inherit huggorm-generated;
    inherit huggorm-gen huggorm-decl huggorm-dsl;
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
      # The inotify change source. A propagated dependency of
      # huggorm, so the interpreter check and test run against needs
      # it too.
      asyncinotify
    ]
  );
  # The emitted front door, into the working tree.
  #
  # `huggorm/__init__.py` is generated (tasks/064) and the tree keeps
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
      ${frontDoor}
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
  # nanopynix, beside this checkout in the nixidae umbrella or at
  # ~/Code/nanopynix, is hand-written, tested nanobind over the same
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
      root=$(cd "''${1:-.}" && pwd)
      # The nixidae umbrella puts nanopynix beside this checkout.
      for candidate in "$root/../nanopynix" "$HOME/Code/nanopynix"; do
        if [ -d "$candidate/nanopynix-bindings/src" ]; then
          HUGGORM_NANOPYNIX=$(cd "$candidate" && pwd)
          export HUGGORM_NANOPYNIX
          break
        fi
      done
      cd "$root/packages/huggorm-gen/gates"
      if [ -n "''${HUGGORM_NANOPYNIX:-}" ]; then
        echo "--- declaration -> nanobind, against $HUGGORM_NANOPYNIX ---"
        python3 nbcheck.py
      else
        echo "--- declaration -> nanobind: SKIPPED, no nanopynix beside this checkout or at ~/Code/nanopynix ---"
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
  #
  # `frontDoor` first, or the tree's huggorm is a namespace portion
  # and the store's package wins the whole directory. See its comment.
  #
  # PYTEST_DEBUG_TEMPROOT gives this suite its OWN basedir, and that
  # is `tasks/062`. pytest's default is `$TMPDIR/pytest-of-$USER`,
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
      pkgs.grpcurl
      ourPython
    ];
    text = ''
      cd "''${HUGGORM_ROOT:-.}"
      ${frontDoor}
      cd packages/huggorm
      export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
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
          # Derived, not read: there is no manifest.json any more
          # (065). This calls the same function the emitters do.
          #
          # stdout is captured because the derivation narrates its
          # progress there - useful in a build log, and not JSON.
          python3 -c 'import contextlib, io, json
from huggorm_gen.pygen.generate import build_manifest
with contextlib.redirect_stdout(io.StringIO()):
    m = build_manifest()
print(json.dumps(m, indent=2))' | jq .
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
  bindings-src = pkgs.runCommand "huggorm-bindings-src" {
    nativeBuildInputs = [
      (pkgs.python3.withPackages (_: [
        huggorm-gen
        huggorm-decl
        huggorm-dsl
      ]))
    ];
  } ''
    mkdir -p "$out"
    python3 -m huggorm_gen.cppgen.generate "$out"
  '';

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
