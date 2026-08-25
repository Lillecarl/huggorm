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
import pathlib
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


def check_no_hardcoded_domain_types():
    """No layer above the bindings may name a domain type.

    Wire policy is declared next to the binding and reaches the schema,
    the server and the client through the manifest. A type name written
    into any of them is the duplication this design exists to remove:
    it means adding a class needs edits in four places, and forgetting
    one fails at the first call that touches it, not at build time."""
    import ast

    from fake_library_python import grpc_pb

    manifest = grpc_pb.load_manifest()
    domain = {
        name
        for group in ("wrappers", "returned_types")
        for name in manifest[group]
    }
    here = pathlib.Path(__file__).parent / "fake_library_python"
    offenders = []
    for mod in ("server.py", "remote.py", "wire.py", "lifecycle.py", "grpc_pb.py"):
        tree = ast.parse((here / mod).read_text(), filename=mod)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and node.value in domain:
                offenders.append(f"{mod}:{node.lineno}: {node.value!r}")
    check("no domain type names above the bindings", not offenders,
          "; ".join(offenders))


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

        check_no_hardcoded_domain_types()

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

        # Regression: set_env used to ship 'Any' params - alive locally,
        # uncallable over the wire. The env-count delta in describe()
        # proves the call landed on the real object.
        before_desc = await drv.describe()
        await asyncio.wait_for(drv.set_env("wire_added", "1"), 10)
        after_desc = await drv.describe()
        def _env_count(d):
            return int(d.split("(")[1].split()[0])
        check("backfilled Any params call over the wire",
              _env_count(after_desc) == _env_count(before_desc) + 1,
              f"{before_desc!r} -> {after_desc!r}")

        state = await client.acquire("EvalState", "local")
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

        # bint-returning methods cross as real booleans (regression:
        # 'bint' used to leak into the schema and map to an opaque
        # Handle, killing the RPC server-side).
        check("bint rpc returns bool over the wire",
              await asyncio.wait_for(v.is_gc_managed(), 10) is True)

        # ---- wire error fidelity -----------------------------------------
        # A C++ failure crosses as a rebuilt InternalError whose decoded
        # cause SURVIVES: __cause__ must be the original ValueError,
        # not None (regression guard for `raise ... from None`).
        bad_path = await rstore.add_text_to_store("plain.txt", "x")
        try:
            await rstore.query_derivation(bad_path)
            raise AssertionError("expected remote InternalError")
        except InternalError as e:
            check("decoded cause survives the wire",
                  type(e.__cause__) is ValueError, type(e.__cause__).__name__)
            check("cause chain re-serializes",
                  e.to_dict()["cause_type"] == "ValueError", e.to_dict())

        # Non-WrapperError server exceptions arrive typed too: unknown
        # and released handles come back as InternalError over KeyError.
        tmp = await client.acquire("LocalStore")
        ghost_id = tmp.handle_id
        await client.release(tmp)
        ghost = remote.RemoteObj(client, "LocalStore", ghost_id)
        threw = None
        try:
            await ghost.get_uri()
        except InternalError as e:
            threw = e.to_dict()
        check("released handle fails typed",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        phantom = remote.RemoteObj(client, "LocalStore", "0" * 32)
        threw = None
        try:
            await phantom.get_uri()
        except InternalError as e:
            threw = e.to_dict()
        check("unknown handle fails typed",
              threw is not None and threw["cause_type"] == "KeyError", threw)

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

            # Construction is a typed rpc on the class's own service now,
            # so an external tool sees the constructor's parameters in
            # reflection instead of a free-text class name.
            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.LocalStoreService/Acquire",
                payload='{}')
            handle = json.loads(out)["id"]
            check("grpcurl typed Acquire parses as JSON", len(handle) == 32, handle[:8])

            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.EvalStateService/Acquire",
                payload='{"store_uri":"local"}')
            check("grpcurl Acquire takes constructor arguments",
                  len(json.loads(out).get("id", "")) == 32, out[:120])

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
