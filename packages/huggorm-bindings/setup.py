import os
import shlex
import shutil
import subprocess

import nanobind
from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE = os.path.join(HERE, "huggorm_bindings")

# The sources, written before setuptools is told about them.
#
# This used to be a Nix `runCommand` that copied the tree, ran the
# emitter over the copy and handed the result in as `src`. That
# worked, and it put the one step that makes a declaration
# load-bearing outside the package that needs it - so `pip install .`
# in this directory built nothing at all.
#
# It happens here now, at import, for the same reason huggorm-generated
# does it here: setuptools resolves its package list while it builds
# metadata, which is before any command runs. A file that does not
# exist then is a file it will not ship.
#
# HUGGORM_BINDINGS_EMITTED names a tree the emitter already wrote. The
# emitter needs Python 3.14, so a wheel for 3.10 to 3.13 compiles the
# tree a 3.14 run wrote (huggorm#107). The Nix build compiles its
# lane's `bindings-src` the same way, so the emitter runs once per lane
# (huggorm#145). Its module list is the `.cpp`
# files in it, one per module, which is what the emitter writes. The
# imports stay inside the branch: the generator cannot import there.
EMITTED = os.environ.get("HUGGORM_BINDINGS_EMITTED")
if EMITTED:
    # copyfile, not copytree: copytree gives the package directory the
    # mode of a read-only source, and nb_runtime() writes into it.
    os.makedirs(PACKAGE, exist_ok=True)
    for name in os.listdir(EMITTED):
        shutil.copyfile(os.path.join(EMITTED, name), os.path.join(PACKAGE, name))
    MODULES = tuple(sorted(name.removesuffix(".cpp")
                           for name in os.listdir(EMITTED)
                           if name.endswith(".cpp")))
    DECL_INCLUDE = os.environ["HUGGORM_DECL_INCLUDE"]
else:
    import huggorm_decl
    from huggorm_gen.cppgen.generate import main as emit
    from huggorm_gen.cppgen.generate import nanobind_modules

    emit(PACKAGE)
    MODULES = nanobind_modules()
    DECL_INCLUDE = huggorm_decl.include_dir()


def pkg_config(*packages: str) -> dict[str, list[str]]:
    """Compiler and linker flags for a real library, from pkg-config.

    Nix ships nix-store.pc, nix-expr.pc and friends, which carry
    -std=c++23, a Requires chain into nix-util and nlohmann_json, and
    a private link line nobody should be reconstructing by hand
    (huggorm#15)."""
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

# Real Nix, and nothing else. nix-expr for the evaluator, nix-flake
# for flake references, nix-cmd for `lookupFileArg`, and nix-store for
# everything else. nix-cmd registers its own global eval, fetcher and
# flake settings when it loads, beside `settings.hpp`'s; a setting
# written to the global config reaches both. nix-flake
# sits above nix-expr, so nix-expr's chain does not bring it; the
# chain does bring nix-fetchers. pkg-config resolves the Requires
# chain, so nix-util and nlohmann_json arrive without being named
# (huggorm#15).
#
# One line for both, not one per module. There used to be a LIBRARY
# table saying which of the two libraries each module linked, because
# some of them linked a mock. There is one library now, so the table
# said the same word nine times (huggorm#60).
_nix = pkg_config("nix-store", "nix-expr", "nix-flake", "nix-cmd")
# ...plus huggorm-decl, for the headers a DECLARATION names. They
# live with the declarations because that is where the hand-written
# input to this build is: `@needs("huggorm_decl/cpp/eval.hpp")` names
# one, and nothing in THIS directory is hand-written at all.
#
# ...and this directory, for the records headers the emitter writes:
# `huggorm_bindings/path_records.hpp`, which `cpp/logging.hpp`
# includes as well as the units do (huggorm#103).
_nix["include_dirs"] = [DECL_INCLUDE, HERE] + _nix["include_dirs"]

# Every module in the package, through nanobind.
#
# Their C++ is written before this runs, by
# `huggorm_gen.cppgen.generate.main`, straight from the declarations - so
# there is no hand-written source for any of them, and the list of
# modules comes from the same place the emitter reads.
#
# nanobind ships its runtime as SOURCE rather than as a library.
# `build_ext_runtime_once` compiles `nb_combined.cpp` one time and links
# the object into every extension. `ext/robin_map` is nanobind's
# vendored hash map, which its own headers include and its wheel does
# not put on the include path.
def nb_runtime() -> str:
    """nanobind's own runtime, beside our sources.

    Copied rather than named where it lives: setuptools refuses an
    absolute path in `sources`, and nanobind's is in its wheel."""
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
        sources=[f"huggorm_bindings/{module}.cpp"],
        language="c++",
        # Hidden by default, which is what nanobind's own build does:
        # two extensions in one process must not export each other's
        # symbols, and its internals are shared through a capsule
        # rather than through the dynamic linker.
        #
        # `-Werror=switch` is the gate under every emitted switch over
        # a Nix enum. Each one is written with no `default:`, so the
        # day upstream adds an enumerator this build FAILS instead of
        # silently rendering it as something else. It is an error
        # rather than a warning because a warning in a build that
        # prints thousands of lines is a warning nobody reads.
        extra_compile_args=[*flags["extra_compile_args"],
                            "-fvisibility=hidden", "-Werror=switch"],
        include_dirs=flags["include_dirs"],
        library_dirs=flags["library_dirs"],
        libraries=flags["libraries"],
        extra_link_args=flags["extra_link_args"],
    )


class build_ext_runtime_once(build_ext):
    """Compile nanobind's runtime once, not once per extension.

    Each extension still links its own copy, as nanobind's own build
    does: the symbols are hidden, and two extensions share nanobind's
    internals through a capsule. Every extension has the same flags,
    so the first one's flags compile the runtime for all of them."""

    def build_extensions(self) -> None:
        first = self.extensions[0]
        runtime = self.compiler.compile(
            [nb_runtime()],
            output_dir=self.build_temp,
            include_dirs=first.include_dirs,
            debug=self.debug,
            extra_postargs=first.extra_compile_args,
        )
        for ext in self.extensions:
            ext.extra_objects = [*ext.extra_objects, *runtime]
        super().build_extensions()


setup(
    ext_modules=[nanobind_extension(m) for m in MODULES],
    cmdclass={"build_ext": build_ext_runtime_once},
)
