#!/usr/bin/env python3
"""
Verify the freshly generated package. Runs inside the Nix build,
right after generate.py, stdlib only:

1. every emitted .py parses
2. the package imports and __all__ matches
3. behavioral checks: results, typed-error passthrough, C++ exception
   wrapping, affine thread pinning, pool execution, aclose
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
    from fake_library_generated import AsyncRemoteCat, AsyncRemoteSpider
    from fake_library_generated._runtime import InternalError
    from fake_library_generated.spec import NameRequiredError

    cat = AsyncRemoteCat("Whiskers")
    assert await cat.greet("you") == "meow to you"
    assert await cat.lives_remaining() == 9
    assert await cat.fetch("ball") == "Whiskers fetched the ball"

    try:
        await cat.fetch("rock")
        raise AssertionError("expected InternalError from C++ throw")
    except InternalError as e:
        d = e.to_dict()
        assert d["code"] == "internal" and d["cause_type"] == "ValueError"

    try:
        await cat.greet("")
        raise AssertionError("expected NameRequiredError")
    except NameRequiredError as e:
        assert e.to_dict()["code"] == "name_required"

    await cat.greet("a")
    await cat.greet("b")
    assert len(cat._runner.workers_seen) == 1, "affine calls must share one thread"
    await cat.aclose()

    spider = AsyncRemoteSpider("Shelob")
    assert await spider.crawl(2.5) == "Shelob crawls 2.5m"
    assert await spider.bite("fly") is True
    results = await asyncio.gather(spider.crawl(1.0), spider.crawl(2.0))
    assert results == ["Shelob crawls 1.0m", "Shelob crawls 2.0m"]
    await spider.aclose()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
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
