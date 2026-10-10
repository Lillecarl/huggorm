"""A flake reference as Nix parses one (`flakeref.cc`), with no fetch."""

import pathlib

import pytest
from nixversion import FLAKES_DISABLED

from huggorm_bindings import parse_flake_ref, set_setting
from huggorm_bindings.errors import NixError

pytestmark = pytest.mark.usefixtures("flakes")


def test_the_subdirectory_is_the_dir_attribute() -> None:
    ref = parse_flake_ref("github:o/r?dir=sub")
    assert (ref.to_string(), ref.subdir()) == ("github:o/r?dir=sub", "sub")
    assert ref.to_attrs() == {"type": "github", "owner": "o", "repo": "r",
                              "dir": "sub"}


def test_an_attribute_keeps_its_arm() -> None:
    """A flag and a count come back as a bool and an int, not as text:
    upstream holds them as `Explicit<bool>` and `uint64_t`."""
    flag = parse_flake_ref("git+https://example.org/r?submodules=1").to_attrs()
    assert (type(flag["submodules"]), flag["submodules"]) == (bool, True)
    count = parse_flake_ref("path:/x?lastModified=5").to_attrs()
    assert (type(count["lastModified"]), count["lastModified"]) == (int, 5)


def test_a_relative_path_parses_against_the_base(tmp_path: pathlib.Path) -> None:
    (tmp_path / "f").mkdir()
    (tmp_path / "f" / "flake.nix").write_text("{ outputs = _: { }; }")
    assert parse_flake_ref("./f", base=str(tmp_path)).to_string() \
        == f"path:{tmp_path}/f"


def test_equal_references_compare_equal() -> None:
    assert parse_flake_ref("github:o/r") == parse_flake_ref("github:o/r")
    assert parse_flake_ref("github:o/r") != parse_flake_ref("github:o/s")


def test_the_feature_is_required() -> None:
    set_setting("experimental-features", "")
    with pytest.raises(NixError, match=FLAKES_DISABLED):
        parse_flake_ref("github:o/r")
