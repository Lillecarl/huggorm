{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # fake-library should be a C++ project with "complex types", it doesn't have to do anything useful
  fake-library = pkgs.callPackage ./fake-library { };
  # this is Cython bindings into fake-library, should contain pxd and pyx (I believe)
  cythonix-bindings = pkgs.callPackage ./cythonix-bindings { inherit fake-library; };
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
    ];
    text = ''
      cd "''${1:-.}"
      echo "--- lint ---"
      ruff check --no-cache cythonix cythonix-generated cythonix-bindings
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
      echo "all checks passed"
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
