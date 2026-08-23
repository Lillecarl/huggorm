{
  lib,
  python3Packages,
  fake-library,
  boehmgc,
  ...
}:
python3Packages.buildPythonPackage {
  pname = "fake-library-bindings";
  version = "0.1.0";
  pyproject = true;
  src = ./.;

  build-system = with python3Packages; [
    setuptools
    cython
  ];

  # boehmgc headers must be visible when compiling the extension: the
  # gc-enabled library and every consumer TU must agree on the alias in
  # gc-env.hpp, or implicit destructors get instantiated twice with two
  # different allocators (an ODR split that frees GC memory with free()).
  buildInputs = [ fake-library boehmgc ];

  # Propagate fake-library so downstream (fake-library-python, ourPython)
  # gets the .so at runtime via rpath + propagatedBuildInputs
  propagatedBuildInputs = [ fake-library ];

  # Tell setup.py where to find headers/libs
  env.FAKE_LIBRARY = "${fake-library}";

  # Also ensure the compiler can find it via CFLAGS/LDFLAGS if setup.py didn't
  # (but we already handle it in setup.py)
  # We also need to make the .pxd files available for downstream Cython cimport
  # buildPythonPackage handles that automatically.

  # Don't run `pip check` that might fail
  pythonImportsCheck = [ "fake_library" ];
}
