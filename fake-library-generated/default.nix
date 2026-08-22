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

  # Codegen happens before setuptools builds. We use `ast` (stdlib) so no
  # extra deps and we get syntax-checked output via ast.unparse.
  preBuild = ''
    echo "=== AST codegen: generating fake_library_generated/ ==="
    export PYTHONPATH=$PYTHONPATH:${fake-library-bindings}/${python3Packages.python.sitePackages}
    ${python3Packages.python.interpreter} generator/generate.py --out fake_library_generated
    echo "--- generated files ---"
    ls -R fake_library_generated
    echo "--- example output ---"
    cat fake_library_generated/__init__.py
    cat fake_library_generated/manifest.json || true
  '';

  pythonImportsCheck = [ "fake_library_generated" ];
}
