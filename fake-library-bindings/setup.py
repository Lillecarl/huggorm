import os
from setuptools import setup, Extension

# Nix will set FAKE_LIBRARY env to the fake-library derivation.
# Fallback to /nix/store lookup is not needed; we error if missing.
fake_lib = os.environ.get("FAKE_LIBRARY")
if not fake_lib:
    # For ad-hoc `pip install -e .` outside Nix, try to find via pkg-config
    # but we keep it simple and require the env.
    raise RuntimeError("FAKE_LIBRARY env var not set — build via Nix, or set FAKE_LIBRARY=/path/to/fake-library")

ext = Extension(
    "fake_library.store",
    sources=["fake_library/store.pyx"],
    language="c++",
    include_dirs=[os.path.join(fake_lib, "include")],
    library_dirs=[os.path.join(fake_lib, "lib")],
    libraries=["fake_library"],
    extra_compile_args=["-std=c++17"],
    extra_link_args=[f"-Wl,-rpath,{os.path.join(fake_lib, 'lib')}"],
)

setup(
    ext_modules=[ext],
)
