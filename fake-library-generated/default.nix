{
  lib,
  python3Packages,
  fake-library-bindings,
  ruff,
  zuban,
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

    build-system = [ python3Packages.setuptools python3Packages.protobuf ];

    # The generator produces every other surface in this repo, so it is
    # the last place a silent Any should survive. Checked against its
    # own source rather than the installed copy: this package IS the
    # generator.
    # The generator only IMPORTS these - it parses pxd files with
    # Cython's own parser, builds descriptors with protobuf and
    # reflects the installed bindings - so they are check inputs, not
    # runtime ones. Without them the checker cannot see what any of
    # those calls return.
    nativeCheckInputs = [
      ruff
      zuban
      python3Packages.cython
      python3Packages.protobuf
      fake-library-bindings
    ];

    # smoke_test is excluded here and checked in the package below:
    # it imports fake_library_generated, which is what the generator
    # PRODUCES, so it does not exist yet at this point in the graph.
    checkPhase = ''
      runHook preCheck
      ruff check --no-cache --config ${../ruff.toml} src
      zuban mypy --strict \
        --python-executable ${python3Packages.python.interpreter} \
        --exclude 'smoke_test\.py$' \
        src/codegen
      runHook postCheck
    '';

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
  # codegen parses each .pxd with Cython's own parser.
  # PXD_FILE lists the bindings' declaration files so returned types
  # derive from pxd usage.
  build-system = [
    python3Packages.setuptools
    python3Packages.cython
    python3Packages.protobuf
    codegen
    fake-library-bindings
  ];

  env.PXD_FILE = "${fake-library-bindings.src}/fake_library/c_store.pxd ${fake-library-bindings.src}/fake_library/c_eval.pxd";

  propagatedBuildInputs = [ fake-library-bindings ];

  # The emitted package and the stubs, checked as a pair. This is the
  # claim tasks/017 and tasks/027 make - that a consumer can be
  # typechecked against the generated protocols - so it is worth
  # holding rather than asserting.
  #
  # PYTHONPATH carries the build directory because the stub package is
  # not installed yet, and a PEP 561 <pkg>-stubs directory is found
  # only through an interpreter's search path (tasks/027).
  nativeCheckInputs = [ zuban ];

  checkPhase = ''
    runHook preCheck
    export PYTHONPATH="$PWD''${PYTHONPATH:+:$PYTHONPATH}"
    zuban mypy --strict \
      --python-executable ${python3Packages.python.interpreter} \
      fake_library_generated
    runHook postCheck
  '';

  pythonImportsCheck = [ "fake_library_generated" ];
}
