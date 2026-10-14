"""What the packages carry that they did not write.

`huggorm-bindings` ships Nix's `get-env.sh` verbatim beside the
bindings, because Nix compiles it into the `nix` binary where no
library can reach it. The NOTICE beside it names whose terms it
travels under, and this holds it there in both lanes that carry
it: the dev install and the wheel.

The manylinux wheel bundles every library the extension links, under
`huggorm_bindings.libs/`. `licenses/<name>/` beside the bindings
holds each library's licence text from the source the image built,
and this holds those there in both lanes too.
"""

import importlib
import importlib.resources
import pathlib

import pytest


def test_the_package_names_whose_terms_the_script_travels_under() -> None:
    """`get-env.sh` is Nix's file under LGPL-2.1, and the NOTICE says so."""
    notice = importlib.resources.files("huggorm_bindings") / "NOTICE"
    text = notice.read_text()
    assert "get-env.sh" in text
    assert "Lesser General Public License" in text


def test_the_licence_text_travels_with_the_script() -> None:
    """LGPL-2.1 section 1: a copy goes out with the licence's text.

    Nix's own `COPYING`, so the text is the one Nix ships."""
    copying = importlib.resources.files("huggorm_bindings") / "COPYING.nix"
    text = copying.read_text()
    assert "GNU LESSER GENERAL PUBLIC LICENSE" in text
    assert "Version 2.1" in text


# Every library the manylinux wheel bundles, by the directory
# `nix/licenses.nix` gives it under `huggorm_bindings/licenses/`.
# nlohmann_json and toml11 are header-only: no object names them, so
# the object test below cannot see them, and only this set holds them.
EXPECTED_LICENSES = frozenset(
    [
        "bzip2",
        "xz",
        "zstd",
        "brotli",
        "libsodium",
        "sqlite",
        "openssl",
        "nghttp2",
        "curl",
        "libarchive",
        "libgit2",
        "boost",
        "libblake3",
        "nlohmann_json",
        "toml11",
        "editline",
        "boehmgc",
    ]
)


def _ships_licence(name: str) -> bool:
    """True when `licenses/<name>/` holds at least one file."""
    directory = importlib.resources.files("huggorm_bindings") / "licenses" / name
    return directory.is_dir() and any(directory.iterdir())


def test_every_bundled_library_ships_its_licence_text() -> None:
    """Each of the seventeen libraries has a non-empty licence directory."""
    missing = sorted(name for name in EXPECTED_LICENSES if not _ships_licence(name))
    assert not missing, f"no licence text ships for: {missing}"


def _library_of(object_name: str) -> str | None:
    """The library an auditwheel object name names, or None for Nix.

    auditwheel keeps the library's own stem before the first `-`:
    `libcurl-abc.so.4` is curl. Several stems name one library, and
    `libnix*` is Nix, whose text travels as `COPYING.nix` above."""
    stem = object_name.split("-")[0]
    if stem.startswith("libnix"):
        return None
    if stem.startswith("libbrotli"):
        return "brotli"
    if stem.startswith("libboost_"):
        return "boost"
    return {
        "libbz2": "bzip2",
        "liblzma": "xz",
        "libzstd": "zstd",
        "libsodium": "libsodium",
        "libsqlite3": "sqlite",
        "libssl": "openssl",
        "libcrypto": "openssl",
        "libnghttp2": "nghttp2",
        "libcurl": "curl",
        "libarchive": "libarchive",
        "libgit2": "libgit2",
        "libblake3": "libblake3",
        "libedit": "editline",
        "libgc": "boehmgc",
    }[stem]


def test_every_bundled_object_maps_to_a_shipped_licence() -> None:
    """Each object in `huggorm_bindings.libs/` has its licence text.

    An object no stem names fails here by name, so a new bundled
    library cannot arrive silently. The dev install bundles nothing,
    so there the directory is absent and this passes on the set above.
    """
    filename = importlib.import_module("huggorm_bindings").__file__
    assert filename is not None, "huggorm_bindings has no package file"
    package = pathlib.Path(filename).parent
    libs = package.parent / "huggorm_bindings.libs"
    if not libs.is_dir():
        pytest.skip("this install bundles no libraries")
    libraries: set[str] = set()
    unknown: list[str] = []
    for entry in libs.glob("*.so*"):
        try:
            name = _library_of(entry.name)
        except KeyError:
            unknown.append(entry.name)
            continue
        if name is not None:
            libraries.add(name)
    assert not unknown, f"bundled objects no stem names: {sorted(unknown)}"
    missing = sorted(name for name in libraries if not _ships_licence(name))
    assert not missing, f"bundled with no licence text: {missing}"
