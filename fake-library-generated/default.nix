{
  lib,
  runCommand,
  python3,
  python3Packages,
  fake-library-bindings,
  ...
}:
let
  # The generator is an ordinary Python distribution with entry points.
  # It is stdlib-only; the spec's own `import fake_library` is satisfied
  # by the environment that runs it, not by a dependency edge here.
  codegen = python3Packages.buildPythonPackage {
    pname = "fake-library-codegen";
    version = "0.1.0";
    pyproject = true;
    src = ./generator;

    build-system = [ python3Packages.setuptools ];

    pythonImportsCheck = [ "codegen" ];
  };

  # Environment able to run codegen against our spec: the tool plus the
  # bindings the spec imports. No manual PYTHONPATH anywhere.
  codegenEnv = python3.withPackages (_: [
    codegen
    fake-library-bindings
  ]);

  # Codegen is its own derivation: consumes compiled bindings, produces
  # the package source tree, smoke-gates its own output.
  codegenSrc = runCommand "fake-library-generated-source" {
    nativeBuildInputs = [ codegenEnv ];
  } ''
    mkdir -p $out
    codegen-generate --spec-dir ${./.} --out $out/fake_library_generated
    codegen-smoke --out $out/fake_library_generated
    cp ${./pyproject.toml} $out/pyproject.toml
  '';
in
python3Packages.buildPythonPackage {
  pname = "fake-library-generated";
  version = "0.1.0";
  pyproject = true;
  src = codegenSrc;

  build-system = [ python3Packages.setuptools ];

  propagatedBuildInputs = [ fake-library-bindings ];

  pythonImportsCheck = [ "fake_library_generated" ];
}
