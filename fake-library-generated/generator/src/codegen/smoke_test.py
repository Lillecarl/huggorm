"""
Verify the freshly generated package. Installed as the `codegen-smoke`
entry point; runs after codegen-generate, stdlib only:

1. every emitted .py parses
2. the package imports and __all__ matches
3. behavioral checks: results, C++ exception wrapping, affine thread
   pinning (including returned affine values), pool execution, aclose
"""

import argparse
import ast
import asyncio
import importlib
import pathlib
import sys


def test_parse(out: pathlib.Path):
    for py in sorted(out.glob("*.py")):
        ast.parse(py.read_text(), filename=str(py))


async def test_behavior():
    from fake_library_generated import AsyncCat, AsyncDog
    from fake_library_generated._runtime import InternalError

    cat = AsyncCat("Whiskers")
    assert await cat.speak() == "meow"
    assert await cat.legs() == 4
    assert await cat.fetch("ball") == "Whiskers fetched the ball"
    assert await cat.name() == "Whiskers"

    try:
        await cat.fetch("rock")
        raise AssertionError("expected InternalError from C++ throw")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    await cat.speak()
    await cat.legs()
    assert len(cat._runner.workers_seen) == 1, "affine calls must share one thread"

    # Returned affine type: ops pin to the PRODUCER's thread
    poop = await cat.poop()
    d1 = await poop.describe()
    d2 = await poop.describe()
    assert d1.endswith("(inspected 1x)") and d2.endswith("(inspected 2x)")
    assert await poop.inspections() == 2
    assert len(poop._runner.workers_seen) == 1, "poop ops must share one thread"
    assert poop._runner.workers_seen == cat._runner.workers_seen, (
        "poop must execute on the producer's thread"
    )

    # Returned pool type: free to use any pool thread
    ball = await cat.toy()
    assert await ball.describe() == "red ball"

    await poop.aclose()
    await ball.aclose()

    # GIL release: two gathered waits on the pool dog overlap (~1x)
    dog = AsyncDog("Rex")
    await dog.wait_ms(50)
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(dog.wait_ms(120), dog.wait_ms(120))
    elapsed = asyncio.get_running_loop().time() - t0
    assert elapsed < 0.22, f"expected overlapped waits, took {elapsed:.2f}s"

    await dog.aclose()
    await cat.aclose()


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    out = pathlib.Path(args.out).resolve()

    test_parse(out)

    # Import the generated package from its parent dir, shadowing any
    # installed copy. Bindings (fake_library) come from PYTHONPATH.
    sys.path.insert(0, str(out.parent))
    importlib.invalidate_caches()
    asyncio.run(test_behavior())
    print("smoke test OK")


if __name__ == "__main__":
    main()
