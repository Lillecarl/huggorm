{
  pkgs ? import <nixpkgs> { },
}:
rec {
  inherit pkgs;
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
}
