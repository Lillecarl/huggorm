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
             cythonix test_remote.py test_lifecycle.py )
      echo "--- typecheck: the emitted package ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        "${cythonix-generated}/lib/python3.14/site-packages/cythonix_generated"
      echo "all checks passed"
    '';
  };

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
