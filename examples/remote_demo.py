"""
Remote demo: same async surface as async_demo, but every operation
crosses a gRPC socket. Wire-values come back as real local copies;
proxies stay remote behind handles.

Every object the client hands back is a generated class with real
methods, so the same function can be typed against a protocol and take
either an in-process wrapper or one of these.
"""

import asyncio
import tempfile

from huggorm import remote


async def main() -> None:
    async with remote.connect() as client:

        root = tempfile.mkdtemp(prefix="huggorm-demo-")
        store = await client.acquire("Store", root)
        state = await client.acquire("EvalState", "dummy://")

        print("=== wire-value returns are real local objects ===")
        from huggorm_bindings import ContentAddressMethod as CA
        from huggorm_bindings import HashAlgorithm
        p = await store.add_to_store("hello.txt", b"world",
                                     CA.NAR, HashAlgorithm.SHA256)
        print(f"{type(p).__module__}.{type(p).__name__}: {p.to_string()}")
        print("valid:", await store.is_valid_path(p))

        print("\n=== wire-value args cross as copies ===")
        # The StorePath above was rebuilt on this side from its parts.
        # Passing it back encodes it as its parts again, and the far side
        # rebuilds a real nix::StorePath before the call runs.
        info = await store.query_path_info(p)
        print(f"nar_size: {info.nar_size()} path: {info.path().to_string()}")

        print("\n=== proxies stay remote behind handles ===")
        v = await state.make_int(42)
        print("value handle:", v.handle_id[:12], "| wire:", v._wire,
              "| class:", type(v).__name__)
        print("integer:", await v.integer())
        await state.force(await state.parse_expr("7"))  # proxy arg over the wire
        text = await state.eval_expr('"hello over grpc"')
        print(f"eval: {await text.string_value()!r}")

        print("\n=== one function, either location, no branching ===")
        # Typed against the generated protocol. It never asks whether the
        # store answering is in this process or on the far side of the
        # socket - and a typechecker sees the whole surface either way.
        from huggorm_generated import StoreLike

        async def report(s: StoreLike) -> str:
            path = await s.add_to_store("shared.txt", b"either location",
                                        CA.NAR, HashAlgorithm.SHA256)
            return f"{await s.get_uri()}: {path.to_string()}"

        print(" remote:", await report(store))

        from huggorm_generated import AsyncStore
        in_process = AsyncStore(tempfile.mkdtemp(prefix="huggorm-demo-"))
        print(" local: ", await report(in_process))
        await in_process.aclose()

        print("\n=== cleanup ===")
        # aclose() means the same thing on both sides: let the object go.
        await store.aclose()
        await state.aclose()
        print("released")


if __name__ == "__main__":
    asyncio.run(main())
