"""Run the grpclib server and the remote demo against it, in-process."""

import logging

logging.basicConfig(level=logging.ERROR)

# remote_demo sits beside this file rather than in the package, so
# the path has to say so. It used to be `from huggorm import
# remote_demo`, which stopped resolving when the demos moved out.
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import anyio
import remote_demo

from huggorm import server


async def main() -> None:
    # A server that fails to bind raises out of the task group, so the
    # demo never runs against nothing.
    async with anyio.create_task_group() as tg:
        tg.start_soon(server.serve, "127.0.0.1", 50051, 120.0)
        await anyio.sleep(0.5)
        await remote_demo.main()
        tg.cancel_scope.cancel()


if __name__ == "__main__":
    anyio.run(main)
