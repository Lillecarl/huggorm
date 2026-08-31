{
  lib,
  python3Packages,
  huggorm-bindings,
  zuban,
  # The declarations, and the reader that turns one into a manifest
  # entry. A build input: pygen imports it and calls it instead of
  # reflecting on a compiled class.
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "huggorm-generated";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  # setup.py imports huggorm_gen.pygen to run it, and the generated
  # package imports huggorm_bindings - both are standard build
  # requirements. protobuf is here rather than in huggorm-gen's own
  # dependencies: it is pygen's extra, and this is the build that
  # uses pygen.
  build-system = [
    python3Packages.setuptools
    python3Packages.protobuf
    huggorm-gen
    huggorm-bindings
    huggorm-decl
    huggorm-dsl
    python3Packages.anyio
  ];


  # anyio because a generated wrapper hands back the async spelling of
  # a type when the bindings declare one: Store.real_path returns a
  # pathlib.Path in process and an anyio.Path from the wrapper, so the
  # emitted module imports anyio (tasks/040).
  propagatedBuildInputs = [ huggorm-bindings python3Packages.anyio ];

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
      huggorm_generated
    runHook postCheck
  '';

  pythonImportsCheck = [ "huggorm_generated" ];
}
