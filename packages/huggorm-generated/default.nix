{
  lib,
  python3Packages,
  huggorm-bindings,
  zuban,
  ruff,
  # The declarations, and the reader that turns them into the model.
  # A build input: pygen imports it and calls it instead of reflecting
  # on a compiled class.
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

  # setup.py imports huggorm_gen.pygen to run it.
  #
  # huggorm-bindings is NOT a build requirement any more. The
  # generator derives every surface from the declarations, so nothing
  # here needs the compiled package to be built first (065). It is
  # still propagated below, because the wrappers this build WRITES
  # import it at run time - and setup.py asserts that generation
  # never touched it.
  build-system = [
    python3Packages.setuptools
    huggorm-gen
    huggorm-decl
    huggorm-dsl
    python3Packages.anyio
  ];


  # anyio because a generated wrapper hands back the async spelling of
  # a type when the bindings declare one: Store.real_path returns a
  # pathlib.Path in process and an anyio.Path from the wrapper, so the
  # emitted module imports anyio (huggorm#40).
  propagatedBuildInputs = [ huggorm-bindings python3Packages.anyio ];

  # pygen lays its output out with `ruff format`.
  nativeBuildInputs = [ ruff ];

  # The surface describes the Nix the bindings link (huggorm#55).
  env.HUGGORM_NIX_VERSION = huggorm-bindings.nixVersion;

  # The emitted package and the stubs, checked as a pair. This is the
  # claim huggorm#17 and huggorm#27 make - that a consumer can be
  # typechecked against the generated protocols - so it is worth
  # holding rather than asserting.
  #
  # PYTHONPATH carries the build directory because the stub package is
  # not installed yet, and a PEP 561 <pkg>-stubs directory is found
  # only through an interpreter's search path (huggorm#27).
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
