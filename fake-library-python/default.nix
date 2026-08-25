{
  lib,
  python3Packages,
  fake-library,
  fake-library-bindings,
  fake-library-generated,
  grpcurl,
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

  # The integration suites used to run only when someone typed
  # `nix run --file . ourPython -- test_remote.py`. Nothing built them,
  # so a green suite proved nothing about the last commit. They run
  # here now, over a real gRPC socket on loopback.
  #
  # grpcurl is the external-tool arm: test_remote drives the same
  # reflection-served schema from outside Python, which is what keeps
  # the emitted descriptors honest.
  nativeCheckInputs = [ grpcurl ];

  checkPhase = ''
    runHook preCheck
    export HOME=$TMPDIR
    echo "--- test_remote ---"
    ${python3Packages.python.interpreter} test_remote.py
    echo "--- test_lifecycle ---"
    ${python3Packages.python.interpreter} test_lifecycle.py
    runHook postCheck
  '';

  pythonImportsCheck = [
    "fake_library_python"
    "fake_library_generated"
  ];
}
