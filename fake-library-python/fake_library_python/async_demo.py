"""
Demo of generated async in-process wrappers.

    C++ fake-library (sync)
      -> Cython bindings (sync; pxd = C++ mapping, pyx = mechanics)
        -> Nix build-time AST codegen from that mapping
          -> AsyncCat / AsyncDog / AsyncPoop / AsyncBall

Threading policy comes from `_threading` markers on the binding classes:
- Cat  (affine): constructed on + pinned to one dedicated thread
- Dog  (pool): runs on a shared thread pool
"""

import asyncio

from fake_library_generated import AsyncCat, AsyncDog


async def main():
    # Constructor args pass through; object is constructed lazily on its thread.
    cat = AsyncCat("Whiskers")
    dog = AsyncDog("Rex")

    print("=== sequential awaits ===")
    print(await cat.speak())
    print(await cat.fetch("ball"))
    print(await dog.legs())

    print("\n=== GIL released during slow C++ calls ===")
    # Heartbeat proves the event loop stays live while a worker thread
    # sits inside the nogil C++ sleep. If the GIL were held, no ticks.
    async def heartbeat():
        while True:
            await asyncio.sleep(0.05)
            yield

    ticks = 0
    async def tick():
        nonlocal ticks
        ticks += 1
        await asyncio.sleep(0.05)

    t0 = asyncio.get_running_loop().time()
    ticker = asyncio.create_task(tick())
    await dog.wait_ms(400)
    elapsed = asyncio.get_running_loop().time() - t0
    ticker.cancel()
    print(f"wait_ms(400) took {elapsed * 1000:.0f}ms, heartbeat ticks: {ticks}")

    # Pool policy + nogil => two waits genuinely overlap
    t0 = asyncio.get_running_loop().time()
    await asyncio.gather(dog.wait_ms(400), dog.wait_ms(400))
    both = asyncio.get_running_loop().time() - t0
    print(f"2x wait_ms(400) gathered: {both * 1000:.0f}ms (parallel if << 800)")

    # Affine policy serializes by design, even though the GIL is released
    t0 = asyncio.get_running_loop().time()
    await cat.wait_ms(250)
    await cat.wait_ms(250)
    serial = asyncio.get_running_loop().time() - t0
    print(f"2x cat wait_ms(250): {serial * 1000:.0f}ms (>=500: one dedicated thread)")

    print("\n=== thread affinity (Cat, affine) ===")
    await cat.speak()
    await cat.fetch("ball")
    print(f"born on:   {cat._runner.born_thread_name}")
    print(f"last call: {cat._runner.last_worker_name}")
    print(f"workers seen: {sorted(cat._runner.workers_seen)}  <- must be exactly 1")

    print("\n=== thread pool (Dog, pool) ===")
    results = await asyncio.gather(
        dog.name(),
        dog.speak(),
        dog.legs(),
    )
    print(f"results: {results}")
    print(f"workers seen: {sorted(dog._runner.workers_seen)}")

    print("\n=== concurrency works (asyncio) ===")
    both = await asyncio.gather(cat.speak(), dog.speak())
    print(f"{both}")

    print("\n=== returned values inherit threading ===")
    poop = await cat.poop()
    ball = await cat.toy()
    print(await poop.describe())
    print(await poop.describe())
    print(f"poop workers: {sorted(poop._runner.workers_seen)} (cat's: {sorted(cat._runner.workers_seen)})")
    print(f"inspections: {await poop.inspections()}")
    print(f"{await ball.describe()} on {sorted(ball._runner.workers_seen)}")
    await asyncio.gather(poop.aclose(), ball.aclose())

    print("\n=== C++ exception surfaces as InternalError with cause chain ===")
    try:
        await cat.fetch("rock")
    except Exception as e:
        print(f"caught {type(e).__name__}: {e.to_dict()}")

    print("\n=== construction failure is cached, not retried ===")
    from fake_library_generated._runtime import InternalError

    # Cython Cat.__cinit__ requires a name; this factory always fails.
    bad = AsyncCat()
    seen = []
    for attempt in (1, 2, 3):
        try:
            await bad.speak()
        except InternalError as e:
            seen.append(e)
            print(f"attempt {attempt}: {type(e).__name__} <- {type(e.__cause__).__name__}: {e.__cause__}")
    print(f"fresh wrapper each call: {seen[0] is not seen[1]}; single cached root cause: {seen[0].__cause__ is seen[2].__cause__}")

    await asyncio.gather(cat.aclose(), dog.aclose())
    print("closed cleanly")


if __name__ == "__main__":
    asyncio.run(main())
