{
  lib,
  python3Packages,
  fake-library,
  fake-library-bindings,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "fake-library-python";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [ setuptools ];

  propagatedBuildInputs = [ fake-library-bindings ];

  pythonImportsCheck = [ "fake_library_python" ];
}
