{
  lib,
  python3Packages,
  fake-library-bindings,
  ...
}:
let
  # The generator is an ordinary Python distribution. It is stdlib-only;
  # the spec's `import fake_library` is satisfied by declaring
  # fake-library-bindings in build-system of the package below, which is
  # exactly how setuptools PEP 517 build requirements are meant to work.
  codegen = python3Packages.buildPythonPackage {
    pname = "fake-library-codegen";
    version = "0.1.0";
    pyproject = true;
    src = ./generator;

    build-system = [ python3Packages.setuptools ];

    pythonImportsCheck = [ "codegen" ];
  };
in
python3Packages.buildPythonPackage {
  pname = "fake-library-generated";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  # setup.py's build_py hook imports codegen to run it, and the generated
  # package imports fake_library - both are standard build requirements.
  # codegen additionally parses c_store.pxd with Cython's own parser.
  # PXD_FILE points the generator at the bindings' declaration file so
  # returned types derive from pxd usage.
  build-system = [
    python3Packages.setuptools
    python3Packages.cython
    codegen
    fake-library-bindings
  ];

  env.PXD_FILE = "${fake-library-bindings.src}/fake_library/c_store.pxd";

  propagatedBuildInputs = [ fake-library-bindings ];

  pythonImportsCheck = [ "fake_library_generated" ];
}
