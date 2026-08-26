import os
import shlex
import shutil
import subprocess

import nanobind
from setuptools import Extension, setup

from cythonix_idl.generate import nanobind_modules

HERE = os.path.dirname(os.path.abspath(__file__))


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
_nix["include_dirs"] = [HERE] + _nix["include_dirs"]

# The REAL Nix bindings, through nanobind rather than Cython.
#
# Their C++ is written before this runs, by
# `cythonix_idl.generate.main`, straight from the declarations - so
# there is no .pyx, no .pxd and no shim header for either of them,
# and the list of modules comes from the same place the emitter reads.
#
# nanobind ships its runtime as SOURCE rather than as a library, so
# each extension compiles `nb_combined.cpp` beside its own
# translation unit. `ext/robin_map` is nanobind's vendored hash map,
# which its own headers include and its wheel does not put on the
# include path.
def nb_runtime() -> str:
    """nanobind's own runtime, beside our sources.

    Copied rather than named where it lives: setuptools refuses an
    absolute path in `sources`, and nanobind's is in its wheel. One
    file, and it compiles into each extension."""
    name = "_nb_combined.cpp"
    target = os.path.join(HERE, "cythonix_bindings", name)
    shutil.copyfile(os.path.join(nanobind.source_dir(), "nb_combined.cpp"),
                    target)
    return f"cythonix_bindings/{name}"


def nanobind_extension(module: str) -> Extension:
    inc = nanobind.include_dir()
    flags = dict(_nix)
    flags["include_dirs"] = [
        inc,
        os.path.join(inc, "..", "ext", "robin_map", "include"),
        # ...and nanobind's own src, because nb_combined.cpp includes
        # its siblings by bare name and the copy above leaves them
        # behind in the wheel.
        nanobind.source_dir(),
        *flags["include_dirs"],
    ]
    return Extension(
        f"cythonix_bindings.{module}",
        sources=[f"cythonix_bindings/{module}.cpp", nb_runtime()],
        language="c++",
        # Hidden by default, which is what nanobind's own build does:
        # two extensions in one process must not export each other's
        # symbols, and its internals are shared through a capsule
        # rather than through the dynamic linker.
        extra_compile_args=[*flags.pop("extra_compile_args", []),
                            "-fvisibility=hidden"],
        **flags,
    )


setup(
    ext_modules=[ext, ext_eval,
                 *[nanobind_extension(m) for m in nanobind_modules()]],
)
