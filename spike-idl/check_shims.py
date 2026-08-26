"""
What cyshims assumes about Cython, asserted rather than commented.

The shim replaces a module Cython owns. That is the right technique -
Cython does it to itself for `libc.math` - and it is also the kind of
thing a release can break silently, in a way that shows up as a
generator reading nothing rather than as an error.

So every assumption is a check. Run it:

    nix run --file .. ourPython -- check_shims.py

A failure here means Cython changed, not that a binding is wrong. The
message says which assumption went.
"""

import inspect
import sys


def test_shadow_still_has_the_mock_we_replace() -> None:
    """cyshims exists because of one specific thing in Shadow.py."""
    import Cython.Shadow

    assert hasattr(Cython.Shadow, "CythonCImports"), (
        "Cython.Shadow.CythonCImports is gone. cyshims replaces it; if "
        "upstream restructured cimports, re-derive the shim rather than "
        "patching around whatever replaced it.")


def test_the_dunder_refusal_is_still_why() -> None:
    """The reason the mock cannot resolve a submodule on its own.

    `CythonCImports.__getattr__` ends in an import_module that would
    do exactly what is wanted - but it refuses every dunder first, so
    Python's import machinery never gets to ask for __spec__. If that
    guard goes, most of cyshims becomes unnecessary."""
    from Cython.Shadow import CythonCImports

    src = inspect.getsource(CythonCImports.__getattr__)
    assert "startswith('__')" in src or 'startswith("__")' in src, (
        "CythonCImports.__getattr__ no longer refuses dunders. Check "
        "whether cyshims is still needed at all.")


def test_cimports_resolves_to_a_plain_module() -> None:
    """The whole trick, end to end.

    `cython.cimports.X` must find the ordinary `X.py`, because that
    file is both the C++ declaration a generator reads and the thing
    that makes a pure-mode binding importable."""
    import pathlib

    import cyshims

    cyshims.declarations(pathlib.Path(__file__).parent / "probe")
    from cython.cimports.c_thing import CThing  # noqa: PLC0415

    assert inspect.isclass(CThing) and CThing.__doc__, (
        "cython.cimports.<name> did not resolve to the plain twin. The "
        "__path__ install is what makes a submodule search reach an "
        "ordinary file.")

    # ...and a name with no twin fails, rather than resolving to some
    # unrelated top-level module. That is what scoping __path__ to the
    # declaration directories buys over pointing it at sys.path.
    try:
        from cython.cimports.inspect import isclass  # noqa: F401, PLC0415
    except ImportError:
        pass
    else:
        raise AssertionError(
            "cython.cimports reached a module that is not a declaration "
            "twin; __path__ is too wide")


def test_a_template_keeps_its_parameter() -> None:
    """"It imports" is not enough; the type has to read back.

    A stand-in that answered `shared_ptr` for `shared_ptr[CStorePath]`
    would import fine and be useless: the parameter is exactly what a
    generator needs to write the declaration out again."""
    import cyshims  # noqa: F401
    from cython.cimports.libcpp.memory import shared_ptr
    from cython.cimports.libcpp.vector import vector

    class CStorePath:
        pass

    assert shared_ptr[CStorePath].spelling() == "shared_ptr[CStorePath]"
    assert vector[shared_ptr[CStorePath]].spelling() == (
        "vector[shared_ptr[CStorePath]]")
    assert shared_ptr.spelling() == "shared_ptr", "bare, with no parameter"


def test_operator_is_still_missing_from_shadow() -> None:
    """cython.operator is the other one Shadow leaves out."""
    import cyshims  # noqa: F401
    from cython.operator import dereference

    assert dereference(7) == 7, (
        "the stand-in should answer the thing itself: interpreted, "
        "nothing is being run, so a raise would be a worse lie than "
        "identity")


def test_cfunc_is_still_erased() -> None:
    """A LIMITATION, asserted so that fixing it is news.

    Shadow makes @cython.cfunc a no-op decorator, so a generator
    reading a pure-mode file by import cannot tell which methods are
    cdef - which is why store.pxd would have to declare each one by
    hand. If Cython ever starts marking them, this fails and the
    design gets simpler."""
    import cython

    def probe() -> None:
        pass

    assert cython.cfunc(probe) is probe, (
        "@cython.cfunc now leaves a trace. A reading generator could "
        "derive the cdef surface after all - see PURE-MODE.md.")


def main() -> None:
    checks = [fn for name, fn in list(globals().items())
              if name.startswith("test_") and callable(fn)]
    for fn in checks:
        fn()
    print(f"cyshims assumptions hold ({len(checks)} checks)")


if __name__ == "__main__":
    sys.exit(main())
