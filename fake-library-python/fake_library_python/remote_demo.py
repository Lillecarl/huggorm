"""
Remote demo: same async surface as async_demo, but every operation
crosses a gRPC socket. Wire-values come back as real local copies;
proxies stay remote behind handles.
"""

import asyncio

from fake_library_python import remote


async def main():
    client = await remote.connect()

    local = await client.acquire("LocalStore")
    remote_store = await client.acquire("RemoteStore")
    state = await client.acquire("EvalState")

    print("=== wire-value returns are real local objects ===")
    p = await local.add_text_to_store("hello.txt", "world")
    print(f"{type(p).__module__}.{type(p).__name__}: {p.to_string()}")
    print("valid:", await local.is_valid_path(p))

    print("\n=== wire-value args cross as copies ===")
    from fake_library import DerivedPath
    drv_path = await local.add_text_to_store("mysite.drv", "DrvMine")
    req = DerivedPath(drv_path, "out")
    built = await local.build_derivation(req)
    print(f"built: {built.to_string()} valid: {await local.is_valid_path(built)}")

    print("\n=== proxies stay remote behind handles ===")
    drv = await remote_store.query_derivation(await remote_store.add_text_to_store("demo.drv", "DrvDemo"))
    print("drv handle:", drv.handle_id[:12], "| wire:", drv.wire)
    print(await drv.describe())
    await state.force(await state.parse_expr("42"))  # proxy arg over the wire
    v = await state.eval_expr('"hello over grpc"')
    print(f"eval: {await v.string_value()!r}")

    print("\n=== cleanup ===")
    await client.release(local)
    await client.release(remote_store)
    await client.release(state)
    print("released")


if __name__ == "__main__":
    asyncio.run(main())
