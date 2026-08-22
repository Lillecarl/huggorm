{
  lib,
  python3Packages,
  fake-library,
  fake-library-bindings,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "fake-library-generated";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [ setuptools ];

  propagatedBuildInputs = [ fake-library-bindings ];

  # Codegen runs before setuptools builds. model.py extracts the IDL into
  # dicts, emitter.py builds ast trees, generate.py writes files. The
  # smoke test then imports and exercises the fresh package — a broken
  # generator fails the build here, not downstream.
  preBuild = ''
    echo "=== AST codegen: generating fake_library_generated/ ==="
    export PYTHONPATH=$PWD:$PYTHONPATH:${fake-library-bindings}/${python3Packages.python.sitePackages}
    ${python3Packages.python.interpreter} generator/generate.py --out fake_library_generated
    ${python3Packages.python.interpreter} generator/smoke_test.py --out fake_library_generated
  '';

  pythonImportsCheck = [ "fake_library_generated" ];
}
