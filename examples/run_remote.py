"""Run the grpclib server and the remote demo against it, in-process."""

import asyncio
import logging

logging.basicConfig(level=logging.ERROR)

# remote_demo sits beside this file rather than in the package, so
# the path has to say so. It used to be `from huggorm import
# remote_demo`, which stopped resolving when the demos moved out.
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import remote_demo  # noqa: E402

from huggorm import server  # noqa: E402


async def main() -> None:
    srv = asyncio.create_task(server.serve("127.0.0.1", 50051))
    done, _ = await asyncio.wait({srv}, timeout=0.5)
    if srv in done:
        # Bound once: exception() was called twice, and it raises rather
        # than returns if the task was cancelled between the two calls.
        exc = srv.exception()
        if exc is not None:
            raise exc
    try:
        await remote_demo.main()
    finally:
        srv.cancel()


if __name__ == "__main__":
    asyncio.run(main())
