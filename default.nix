{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
  inherit (pkgs) lib;
  # fake-library should be a C++ project with "complex types", it doesn't have to do anything useful
  fake-library = pkgs.callPackage ./fake-library { };
  # this is Cython bindings into fake-library, should contain pxd and pyx (I believe)
  fake-library-bindings = pkgs.callPackage ./fake-library-bindings { inherit fake-library; };
  # this is a Python library that uses fake-library-bindings
  fake-library-python = pkgs.callPackage ./fake-library-python {
    inherit fake-library;
    inherit fake-library-bindings;
    inherit fake-library-generated;
  };
  # AST codegen layer between bindings and python: pxd + live bindings -> generated stubs
  fake-library-generated = pkgs.callPackage ./fake-library-generated {
    inherit fake-library;
    inherit fake-library-bindings;
  };
  # nix run --file . python -- $args
  # to be able to run Python commands
  python = pkgs.python3.withPackages (ps: with ps; [ cython ]);
  # nix run --file . python -- $args
  # to be able to run Python commands with our packages loaded
  ourPython = pkgs.python3.withPackages (
    ps: with ps; [
      cython
      fake-library-bindings
      fake-library-generated
      fake-library-python
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
      ruff check --no-cache fake-library-python fake-library-generated fake-library-bindings
      echo "--- typecheck: the generator ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        --exclude 'smoke_test\.py$' \
        fake-library-generated/generator/src/codegen
      echo "--- typecheck: the hand-written layer and the suites ---"
      ( cd fake-library-python \
        && zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
             fake_library_python test_remote.py test_lifecycle.py )
      echo "--- typecheck: the emitted package ---"
      zuban mypy --strict --python-executable "${ourPython}/bin/python3" \
        "${fake-library-generated}/lib/python3.14/site-packages/fake_library_generated"
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
