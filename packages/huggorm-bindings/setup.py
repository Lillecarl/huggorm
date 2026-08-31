import os
import shlex
import shutil
import subprocess

import nanobind
from setuptools import Extension, setup

from huggorm_gen.cppgen.generate import nanobind_modules

HERE = os.path.dirname(os.path.abspath(__file__))


def pkg_config(*packages: str) -> dict[str, list[str]]:
    """Compiler and linker flags for a real library, from pkg-config.

    Nix ships nix-store.pc, nix-expr.pc and friends, which carry
    -std=c++23, a Requires chain into nix-util and nlohmann_json, and
    a private link line nobody should be reconstructing by hand
    (tasks/015)."""
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

# Real Nix, and nothing else. nix-expr for the evaluator and
# nix-store for everything else; pkg-config resolves the Requires
# chain, so nix-util and nlohmann_json arrive without being named
# (tasks/015).
#
# One line for both, not one per module. There used to be a LIBRARY
# table saying which of the two libraries each module linked, because
# some of them linked a mock. There is one library now, so the table
# said the same word nine times (tasks/060).
_nix = pkg_config("nix-store", "nix-expr")
# ...plus this directory, for the headers in `_cpp/`. They sit beside
# the sources rather than in the extension because they are C++ a
# DECLARATION names: `@binds("huggorm::translate_nix_error")` points
# at one.
_nix["include_dirs"] = [HERE] + _nix["include_dirs"]

# Every module in the package, through nanobind.
#
# Their C++ is written before this runs, by
# `huggorm_gen.cppgen.generate.main`, straight from the declarations - so
# there is no hand-written source for any of them, and the list of
# modules comes from the same place the emitter reads.
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
    target = os.path.join(HERE, "huggorm_bindings", name)
    shutil.copyfile(os.path.join(nanobind.source_dir(), "nb_combined.cpp"),
                    target)
    return f"huggorm_bindings/{name}"


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
        f"huggorm_bindings.{module}",
        sources=[f"huggorm_bindings/{module}.cpp", nb_runtime()],
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
    ext_modules=[nanobind_extension(m) for m in nanobind_modules()],
)
