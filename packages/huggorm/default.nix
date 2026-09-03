{
  lib,
  python3Packages,
  huggorm-bindings,
  huggorm-generated,
  huggorm-gen,
  huggorm-decl,
  huggorm-dsl,
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

  # The generator too, because `setup.py` writes `huggorm/__init__.py`
  # before setuptools resolves the package list. The front door is a
  # mapping of Python names onto the two packages behind it, so it is
  # derived rather than tracked by hand (tasks/064).
  #
  # protobuf comes with it: `build_manifest()` reaches the schema
  # builder, which builds a FileDescriptorSet.
  build-system = with python3Packages; [
    setuptools
    protobuf
    huggorm-gen
    huggorm-decl
    huggorm-dsl
  ];

  # generated is propagated so anyone writing code in/downstream of
  # huggorm sees huggorm_generated in their environment.
  # grpclib: asyncio gRPC transport for the remote layer. protobuf
  # runtime feeds the manifest-built descriptor schema.
  # googleapis-common-protos: google.rpc.Status, which is the message
  # gRPC puts in grpc-status-details-bin - the only place a FAILED
  # call can carry a typed answer (tasks/036).
  # asyncinotify: the kernel telling the watcher a file moved, instead
  # of the watcher stat-ing for it (tasks/083). Linux only, which is
  # what let it beat watchdog - Carl ruled Darwin out for now, so
  # cross-platform bought nothing and cost a thread pool. It
  # propagates nothing but python3 itself.
  propagatedBuildInputs = [
    huggorm-bindings
    huggorm-generated
    python3Packages.asyncinotify
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
    # The generator, for the suite alone. Several tests hold an
    # artifact - the descriptor set, the front door's __all__, the
    # stubs - against what the build decided, and they get that by
    # calling `build_manifest()` rather than by reading a JSON dump of
    # it (065). Not a runtime dependency: nothing in `huggorm/`
    # imports it.
    huggorm-gen
    huggorm-decl
    huggorm-dsl
  ];

  checkPhase = ''
    runHook preCheck
    export HOME=$TMPDIR
    echo "--- lint ---"
    ruff check --no-cache --config ${../../ruff.toml} .
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
