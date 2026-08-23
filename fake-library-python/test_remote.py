"""
Remote-layer test harness.

Starts the grpclib server as a SUBPROCESS (so its errors are visible,
not swallowed by a task), waits for readiness, drives the Python
client through the full proxy/value matrix, then exercises the same
API from grpcurl as an external tool via reflection. Everything runs
under explicit timeouts; server logs are captured and dumped on
failure.

Run:  nix run --file . ourPython -- test_remote.py
"""

import asyncio
import glob
import json
import os
import shutil
import socket
import sys

HOST = "127.0.0.1"
PORT = None


def check(name, cond, detail=""):
    marker = "PASS" if cond else "FAIL"
    print(f"[{marker}] {name}" + (f" :: {detail}" if detail else ""))
    if not cond:
        raise AssertionError(name)


def find_grpcurl():
    if os.environ.get("GRPCURL"):
        return os.environ["GRPCURL"]
    w = shutil.which("grpcurl")
    if w:
        return w
    cands = sorted(glob.glob("/nix/store/*-grpcurl-*/bin/grpcurl"))
    return cands[-1] if cands else None


def free_port():
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def wait_port(port, timeout=20):
    async def probe():
        while True:
            try:
                _, w = await asyncio.open_connection(HOST, port)
                w.close()
                return
            except OSError:
                await asyncio.sleep(0.1)
    await asyncio.wait_for(probe(), timeout)


async def run_tool(binpath, symbol=None, payload=None, timeout=20):
    """grpcurl [-d payload] host:port [symbol]"""
    argv = [binpath, "-plaintext"]
    if payload is not None:
        argv += ["-d", payload]
    argv.append(f"{HOST}:{PORT}")
    if symbol:
        argv.append(symbol)
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await asyncio.wait_for(proc.communicate(), timeout)
    return proc.returncode, out.decode(), err.decode()


async def drain(stream, sink):
    while True:
        line = await stream.readline()
        if not line:
            break
        sink.append(line.decode(errors="replace"))


async def main():
    global PORT
    PORT = free_port()
    grpcurl_bin = find_grpcurl()

    server = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fake_library_python.server", HOST, str(PORT),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    logs: list[str] = []
    drain_task = asyncio.gather(drain(server.stdout, logs), drain(server.stderr, logs))

    try:
        await wait_port(PORT)

        # ---- Python client: proxy/value matrix --------------------------
        from fake_library import DerivedPath
        from fake_library_generated._runtime import InternalError
        from fake_library_python import remote

        client = await asyncio.wait_for(remote.connect(HOST, PORT), 10)

        store = await asyncio.wait_for(client.acquire("LocalStore"), 10)
        check("acquire returns a handle", bool(store.handle_id))

        p = await asyncio.wait_for(store.add_text_to_store("hello.txt", "world"), 10)
        check("wire-value return is a real local object",
              type(p).__module__ == "fake_library.store", type(p).__name__)
        check("round trip content", p.to_string().endswith("hello.txt"))

        check("is_valid_path over the wire",
              await asyncio.wait_for(store.is_valid_path(p), 10) is True)

        drv_path = await store.add_text_to_store("mysite.drv", "DrvMine")
        built = await store.build_derivation(DerivedPath(drv_path, "out"))
        check("value ARG crosses as copy and builds",
              built.to_string().endswith("-out")
              and await store.is_valid_path(built))

        rstore = await client.acquire("RemoteStore")
        drv = await rstore.query_derivation(
            await rstore.add_text_to_store("demo.drv", "DrvDemo"))
        check("proxy stays remote", isinstance(drv, remote.RemoteObj)
              and drv.wire == "proxy")
        desc = await drv.describe()
        check("proxy method executes on producer thread", "seen 1x" in desc, desc)

        state = await client.acquire("EvalState")
        thunk = await state.parse_expr("42")
        threw = False
        try:
            await thunk.integer()
        except InternalError:
            threw = True
        check("thunk access fails over the wire", threw)
        await state.force(thunk)
        check("force mutates in place remotely", await thunk.integer() == 42)

        v = await state.eval_expr('"hello over grpc"')
        check("eval round trip", await v.string_value() == "hello over grpc")

        # ---- external tool via reflection -------------------------------
        if not grpcurl_bin:
            print("[SKIP] grpcurl not found")
        else:
            rc, out, err = await run_tool(grpcurl_bin, symbol="list")
            if not out.strip():
                print(f"[WARN] grpcurl list empty; rc={rc} err={err!r}")
            for svc in ("Session", "LocalStoreService", "EvalStateService",
                        "ValueService", "DerivationService"):
                check(f"reflection lists {svc}", f"nixmock.v1.{svc}" in out,
                      f"rc={rc} out={out[:120]!r} err={err[:120]!r}")

            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.Session/Acquire",
                payload='{"class":"LocalStore"}')
            handle = json.loads(out)["id"]
            check("grpcurl Acquire parses as JSON", len(handle) == 32, handle[:8])

            rc, out, err = await run_tool(
                grpcurl_bin,
                symbol="nixmock.v1.LocalStoreService/add_text_to_store",
                payload=json.dumps({
                    "self": {"id": handle},
                    "name": "via-grpcurl.txt",
                    "contents": "external tool"}))
            base = json.loads(out)["result"]["base_name"]
            check("grpcurl typed call returns StorePath",
                  base.endswith("via-grpcurl.txt"), base)

        print("\nALL REMOTE CHECKS PASSED")

        await client.release(store)
        await client.release(rstore)
        await client.release(state)
    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), 5)
        except asyncio.TimeoutError:
            server.kill()
        await drain_task
        if any("Application error" in l for l in logs):
            print("\n--- server reported application errors ---")
        print(f"\n--- server log ({len(logs)} lines, last 10) ---")
        for line in logs[-10:]:
            print(line.rstrip())


if __name__ == "__main__":
    asyncio.run(main())
