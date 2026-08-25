"""
Remote demo: same async surface as async_demo, but every operation
crosses a gRPC socket. Wire-values come back as real local copies;
proxies stay remote behind handles.

Every object the client hands back is a generated class with real
methods, so the same function can be typed against a protocol and take
either an in-process wrapper or one of these.
"""

import asyncio

from fake_library_python import remote


async def main() -> None:
    client = await remote.connect()

    local = await client.acquire("LocalStore")
    remote_store = await client.acquire("RemoteStore")
    state = await client.acquire("EvalState", "local")

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
    drv = await remote_store.query_derivation(
        await remote_store.add_text_to_store("demo.drv", "DrvDemo"))
    print("drv handle:", drv.handle_id[:12], "| wire:", drv._wire,
          "| class:", type(drv).__name__)
    print(await drv.describe())
    await state.force(await state.parse_expr("42"))  # proxy arg over the wire
    v = await state.eval_expr('"hello over grpc"')
    print(f"eval: {await v.string_value()!r}")

    print("\n=== the abstract base over the wire ===")
    # Shared store methods are declared once, on StoreService, so a
    # caller works a store without knowing which kind answered.
    for h in (local, remote_store):
        print(f"  {type(h).__name__:16} get_uri -> {await h.get_uri()}"
              f"  (via {type(h)._rpc['get_uri']['rpc']['path']})")
    print("  describe(store) over the wire:",
          await client.call_function("describe", local))

    print("\n=== one function, either location, no branching ===")
    # Typed against the generated protocol. It never asks whether the
    # store answering is in this process or on the far side of the
    # socket - and a typechecker sees the whole surface either way.
    from fake_library_generated import StoreLike

    async def report(store: StoreLike) -> str:
        path = await store.add_text_to_store("shared.txt", "either location")
        return f"{await store.get_uri()}: {path.to_string()}"

    print(" remote:", await report(local))

    from fake_library_generated import AsyncLocalStore
    in_process = AsyncLocalStore()
    print(" local: ", await report(in_process))
    await in_process.aclose()

    print("\n=== cleanup ===")
    # aclose() means the same thing on both sides: let the object go.
    await local.aclose()
    await remote_store.aclose()
    await state.aclose()
    print("released")


if __name__ == "__main__":
    asyncio.run(main())
