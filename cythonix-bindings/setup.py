import os
import shlex
import subprocess

from setuptools import Extension, setup


def pkg_config(*packages: str) -> dict[str, list[str]]:
    """Compiler and linker flags for a real library, from pkg-config.

    The mock is found through one FAKE_LIBRARY prefix with `include`
    and `lib` joined onto it. Real Nix is not: it ships nix-store.pc,
    nix-expr.pc and friends, which carry -std=c++23, a Requires chain
    into nix-util and nlohmann_json, and a private link line nobody
    should be reconstructing by hand (tasks/015)."""
    def run(flag: str) -> list[str]:
        out = subprocess.run(["pkg-config", flag, *packages],
                             capture_output=True, text=True, check=True)
        return shlex.split(out.stdout)

    cflags, ldflags = run("--cflags"), run("--libs")
    library_dirs = [f[2:] for f in ldflags if f.startswith("-L")]
    return {
        "include_dirs": [f[2:] for f in cflags if f.startswith("-I")],
        "extra_compile_args": [f for f in cflags if not f.startswith("-I")],
        "library_dirs": library_dirs,
        "libraries": [f[2:] for f in ldflags if f.startswith("-l")],
        # rpath, or the extension imports and then cannot find
        # libnixstore. Nix has no global library path to fall back on.
        "extra_link_args": [f for f in ldflags
                            if not f.startswith(("-L", "-l"))]
        + [f"-Wl,-rpath,{d}" for d in library_dirs],
    }

# Nix will set FAKE_LIBRARY env to the fake-library derivation.
# Fallback to /nix/store lookup is not needed; we error if missing.
fake_lib = os.environ.get("FAKE_LIBRARY")
if not fake_lib:
    # For ad-hoc `pip install -e .` outside Nix, try to find via pkg-config
    # but we keep it simple and require the env.
    raise RuntimeError(
        "FAKE_LIBRARY env var not set - build via Nix, or set "
        "FAKE_LIBRARY=/path/to/fake-library")

ext = Extension(
    "cythonix_bindings.mock_store",
    sources=["cythonix_bindings/mock_store.pyx"],
    language="c++",
    include_dirs=[os.path.join(fake_lib, "include")],
    library_dirs=[os.path.join(fake_lib, "lib")],
    libraries=["fake_library"],
    extra_compile_args=["-std=c++23", "-DFAKE_LIBRARY_USE_BOEHMGC=1"],
    extra_link_args=[f"-Wl,-rpath,{os.path.join(fake_lib, 'lib')}"],
)

ext_eval = Extension(
    "cythonix_bindings.eval",
    sources=["cythonix_bindings/eval.pyx"],
    language="c++",
    include_dirs=[os.path.join(fake_lib, "include")],
    library_dirs=[os.path.join(fake_lib, "lib")],
    libraries=["fake_library"],
    extra_compile_args=["-std=c++23", "-DFAKE_LIBRARY_USE_BOEHMGC=1"],
    extra_link_args=[f"-Wl,-rpath,{os.path.join(fake_lib, 'lib')}"],
)

# The first REAL Nix type, beside the mock rather than replacing it
# (tasks/015). Nothing about it goes through FAKE_LIBRARY.
_nix = pkg_config("nix-store")
# ...plus this directory, for nix_error.hpp. It sits beside the
# sources rather than in the extension because it is C++ that Cython
# calls, not Cython: `except +translate_nix_error` names a function.
_nix["include_dirs"] = [os.path.dirname(os.path.abspath(__file__))] + _nix["include_dirs"]

ext_path = Extension(
    "cythonix_bindings.path",
    sources=["cythonix_bindings/path.pyx"],
    language="c++",
    **_nix,
)

ext_store = Extension(
    "cythonix_bindings.store",
    sources=["cythonix_bindings/store.pyx"],
    language="c++",
    **_nix,
)

setup(
    ext_modules=[ext, ext_eval, ext_path, ext_store],
)
