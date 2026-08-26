"""Run the grpclib server and the remote demo against it, in-process."""

import asyncio
import logging

logging.basicConfig(level=logging.ERROR)

from cythonix import remote_demo, server


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
