{
  lib,
  python3Packages,
  fake-library,
  fake-library-bindings,
  fake-library-generated,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "fake-library-python";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [ setuptools ];

  # generated is propagated so anyone writing code in/downstream of
  # fake-library-python sees fake_library_generated in their environment.
  # grpclib: asyncio gRPC transport for the remote layer. protobuf
  # runtime feeds the manifest-built descriptor schema.
  propagatedBuildInputs = [
    fake-library-bindings
    fake-library-generated
    python3Packages.grpclib
    python3Packages.protobuf
  ];

  pythonImportsCheck = [
    "fake_library_python"
    "fake_library_generated"
  ];
}
