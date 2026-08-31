{
  lib,
  python3Packages,
  huggorm-bindings,
  huggorm-generated,
  grpcurl,
  ruff,
  zuban,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "huggorm";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [ setuptools ];

  # generated is propagated so anyone writing code in/downstream of
  # huggorm sees huggorm_generated in their environment.
  # grpclib: asyncio gRPC transport for the remote layer. protobuf
  # runtime feeds the manifest-built descriptor schema.
  # googleapis-common-protos: google.rpc.Status, which is the message
  # gRPC puts in grpc-status-details-bin - the only place a FAILED
  # call can carry a typed answer (tasks/036).
  propagatedBuildInputs = [
    huggorm-bindings
    huggorm-generated
    python3Packages.googleapis-common-protos
    python3Packages.grpclib
    python3Packages.protobuf
  ];

  # The integration suites run here, over a real gRPC socket on
  # loopback, under pytest. They used to be scripts with a hand-rolled
  # check() and one giant main(), which meant no isolation, no way to
  # run one of them, and a failure that stopped everything after it.
  # That was survivable against a mock; against real Nix, where a
  # failure can be a native crash, it is not.
  #
  # grpcurl is the external-tool arm: test_remote drives the same
  # reflection-served schema from outside Python, which is what keeps
  # the emitted descriptors honest.
  # zuban is a mypy-compatible checker in Rust. Same flags, same
  # diagnostics on this codebase, about thirty times faster - which is
  # what makes it reasonable to run on every build rather than by hand.
  #
  # --python-executable, not MYPYPATH: a PEP 561 <pkg>-stubs package is
  # only found through an interpreter's search path, never through
  # MYPYPATH. Without it every binding type reads as Any and the check
  # passes while proving nothing (tasks/027).
  nativeCheckInputs = [
    grpcurl
    ruff
    zuban
    python3Packages.pytest
    python3Packages.anyio
    python3Packages.pytest-timeout
  ];

  checkPhase = ''
    runHook preCheck
    export HOME=$TMPDIR
    echo "--- lint ---"
    ruff check --no-cache --config ${../ruff.toml} .
    echo "--- typecheck ---"
    zuban mypy --strict \
      --python-executable ${python3Packages.python.interpreter} \
      huggorm tests
    echo "--- pytest (hermetic only) ---"
    # -m "not live": a build sandbox has no daemon, no db and no
    # writable store, so a test that needs one cannot run here. The
    # rest of the suite runs on every build, as it always has. The
    # live half runs from the devshell: nix run --file . test
    # (tasks/037).
    pytest -m "not live"
    runHook postCheck
  '';

  pythonImportsCheck = [
    "huggorm"
    "huggorm_generated"
  ];
}
