{
  lib,
  python3Packages,
  cythonix-bindings,
  ruff,
  zuban,
  # The declarations, and the reader that turns one into a manifest
  # entry. A build input to the generator: `codegen` imports it and
  # calls it instead of reflecting on a compiled class.
  cythonix-idl,
  ...
}:
let
  # The generator is an ordinary Python distribution. It is stdlib-only;
  # the spec's `import cythonix_bindings` is satisfied by declaring
  # cythonix-bindings in build-system of the package below, which is
  # exactly how setuptools PEP 517 build requirements are meant to work.
  codegen = python3Packages.buildPythonPackage {
    pname = "cythonix-codegen";
    version = "0.1.0";
    pyproject = true;
    src = ./generator;

    build-system = [ python3Packages.setuptools python3Packages.protobuf ];

    # The generator produces every other surface in this repo, so it is
    # the last place a silent Any should survive. Checked against its
    # own source rather than the installed copy: this package IS the
    # generator.
    # The generator only IMPORTS these - it builds descriptors with
    # protobuf, reads the declarations through cythonix-idl and
    # enumerates the installed bindings - so they are check inputs,
    # not runtime ones. Without them the checker cannot see what any
    # of those calls return.
    nativeCheckInputs = [
      ruff
      zuban
      python3Packages.protobuf
      cythonix-bindings
      cythonix-idl
    ];

    # smoke_test is excluded here and checked in the package below:
    # it imports cythonix_generated, which is what the generator
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
  pname = "cythonix-generated";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  # setup.py's build_py hook imports codegen to run it, and the generated
  # package imports cythonix_bindings - both are standard build
  # requirements. cythonix-idl is the third: the manifest comes from
  # the declarations, not from parsing a pxd and reflecting on a
  # compiled class.
  build-system = [
    python3Packages.setuptools
    python3Packages.protobuf
    codegen
    cythonix-bindings
    cythonix-idl
    python3Packages.anyio
  ];


  # anyio because a generated wrapper hands back the async spelling of
  # a type when the bindings declare one: Store.real_path returns a
  # pathlib.Path in process and an anyio.Path from the wrapper, so the
  # emitted module imports anyio (tasks/040).
  propagatedBuildInputs = [ cythonix-bindings python3Packages.anyio ];

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
      cythonix_generated
    runHook postCheck
  '';

  pythonImportsCheck = [ "cythonix_generated" ];
}
