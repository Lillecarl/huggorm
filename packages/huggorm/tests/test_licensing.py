"""What the packages carry that they did not write.

`huggorm-bindings` ships Nix's `get-env.sh` verbatim beside the
bindings, because Nix compiles it into the `nix` binary where no
library can reach it. The NOTICE beside it names whose terms it
travels under, and this holds it there in both lanes that carry
it: the dev install and the wheel.
"""

import importlib.resources


def test_the_package_names_whose_terms_the_script_travels_under() -> None:
    """`get-env.sh` is Nix's file under LGPL-2.1, and the NOTICE says so."""
    notice = importlib.resources.files("huggorm_bindings") / "NOTICE"
    text = notice.read_text()
    assert "get-env.sh" in text
    assert "Lesser General Public License" in text
