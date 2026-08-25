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
from typing import Any

HOST = "127.0.0.1"
PORT = None


def check(name: str, cond: Any, detail: Any = "") -> None:
    marker = "PASS" if cond else "FAIL"
    print(f"[{marker}] {name}" + (f" :: {detail}" if detail else ""))
    if not cond:
        raise AssertionError(name)


def find_grpcurl() -> str | None:
    if os.environ.get("GRPCURL"):
        return os.environ["GRPCURL"]
    w = shutil.which("grpcurl")
    if w:
        return w
    cands = sorted(glob.glob("/nix/store/*-grpcurl-*/bin/grpcurl"))
    return cands[-1] if cands else None


def free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


async def wait_port(port: int, timeout: float = 20) -> None:
    async def probe() -> None:
        while True:
            try:
                _, w = await asyncio.open_connection(HOST, port)
                w.close()
                return
            except OSError:
                await asyncio.sleep(0.1)
    await asyncio.wait_for(probe(), timeout)


async def run_tool(binpath: str, symbol: str | None = None,
                   payload: str | None = None,
                   timeout: float = 20) -> tuple[int | None, str, str]:
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


async def drain(stream: Any, sink: list[str]) -> None:
    while True:
        line = await stream.readline()
        if not line:
            break
        sink.append(line.decode(errors="replace"))


def check_no_hardcoded_domain_types() -> None:
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


async def main() -> None:
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
        import fake_library
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
        from fake_library_generated import RPC_CLASSES, RPCDerivation
        check("proxy stays remote", isinstance(drv, RPCDerivation)
              and drv._wire == "proxy")
        # The generated class carries real methods, so a missing one is
        # a plain AttributeError from Python - not a manifest lookup
        # that produced a coroutine either way.
        check("generated client class, not a __getattr__ proxy",
              not hasattr(remote, "RemoteObj")
              and type(drv).__module__ == "fake_library_generated.rpc",
              type(drv).__module__)
        desc = await drv.describe()
        check("proxy method executes on producer thread", "seen 1x" in desc, desc)

        # Regression: set_env used to ship 'Any' params - alive locally,
        # uncallable over the wire. The env-count delta in describe()
        # proves the call landed on the real object.
        before_desc = await drv.describe()
        await asyncio.wait_for(drv.set_env("wire_added", "1"), 10)
        after_desc = await drv.describe()
        def _env_count(d: str) -> int:
            return int(d.split("(")[1].split()[0])
        check("backfilled Any params call over the wire",
              _env_count(after_desc) == _env_count(before_desc) + 1,
              f"{before_desc!r} -> {after_desc!r}")

        state = await client.acquire("EvalState", "local")
        thunk = await state.parse_expr("42")
        threw: Any = False
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
        ghost = client.proxy("LocalStore", ghost_id)
        threw = None
        try:
            await ghost.get_uri()
        except InternalError as e:
            threw = e.to_dict()
        check("released handle fails typed",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        phantom = client.proxy("LocalStore", "0" * 32)
        threw = None
        try:
            await phantom.get_uri()
        except InternalError as e:
            threw = e.to_dict()
        check("unknown handle fails typed",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        # ---- the abstract base over the wire -----------------------------
        # A handle is a handle: the shared surface resolves through
        # StoreService whichever implementation is behind it, and the
        # Python client walks to the base exactly as Python would.
        for kind, want in (("LocalStore", "local"), ("RemoteStore", "uds://daemon")):
            h = await client.acquire(kind)
            check(f"inherited method on {kind}", await h.get_uri() == want)
            check(f"{kind} dispatches get_uri through StoreService",
                  type(h)._rpc["get_uri"]["rpc"]["path"]
                  == "/nixmock.v1.StoreService/get_uri",
                  type(h)._rpc["get_uri"]["rpc"]["path"])
            check(f"{kind} client class inherits from the base's",
                  isinstance(h, RPC_CLASSES["Store"]), type(h).__name__)
            await client.release(h)

        # query_derivation is NOT guaranteed: the pool policy drops it
        # from LocalStore, so it is on RemoteStore alone.
        pool_store = await client.acquire("LocalStore")
        threw = None
        try:
            _ = pool_store.query_derivation
        except AttributeError as e:
            threw = str(e)
        # It is simply not on the class, so Python raises before any
        # call is made. The generated surface and the manifest agree by
        # construction; the conformance gate pins that.
        check("un-guaranteed method absent from the pool subclass",
              threw is not None and "query_derivation" in threw, threw)
        check("...but present on the affine one",
              hasattr(RPC_CLASSES["RemoteStore"], "query_derivation"))
        await client.release(pool_store)

        # A wire-value is not remotely constructible: there is no handle
        # to construct into. It is built locally and passed as an
        # argument, which the build_derivation check above already did.
        threw = None
        try:
            await client.acquire("DerivedPath")
        except ValueError as e:
            threw = str(e)
        check("wire-value refuses remote construction",
              threw is not None and "DerivedPath" in threw, threw)

        # ---- free functions over the wire --------------------------------
        # No handle: a module-level function has no instance. Only the
        # ones the wire can represent are offered, and the others say
        # why (describe takes the excluded Store base, gc_stats returns
        # a dict the schema has no type for).
        check("free function crosses the wire",
              await client.call_function("collect_garbage") is None)
        # describe takes a Store. It had no RPC surface at all until
        # Store became a generated base with a wire identity.
        check("free function takes an abstract-base handle",
              await client.call_function("describe", store) == "store(local)")
        check("...for either implementation",
              await client.call_function("describe", rstore) == "store(uds://daemon)")
        # A handle is resolvable as an ARGUMENT from the moment it
        # exists. The server resolves it to a wrapper whose affine
        # target may not be built yet; that used to refuse, so passing
        # an untouched RemoteStore anywhere failed. Both handles above
        # had been called already, which hid it.
        untouched = await client.acquire("RemoteStore")
        check("an untouched affine handle resolves as an argument",
              await client.call_function("describe", untouched)
              == "store(uds://daemon)")
        await untouched.aclose()
        # Same for a method argument: a proxy produced by one object
        # resolves server-side when passed to another.
        other_state = await client.acquire("EvalState", "local")
        loose = await state.parse_expr("7")
        await other_state.force(loose)
        check("proxy arg resolves against a different remote object",
              await loose.integer() == 7)
        await other_state.aclose()
        # A dict crosses as a protobuf map. Nix attribute names are
        # always strings, so map<string, V> covers every dict this API
        # returns; the value type comes from the declaration, which is
        # the same annotation the typechecker reads (tasks/030).
        stats = await client.call_function("gc_stats")
        check("a dict return crosses as a map",
              isinstance(stats, dict) and stats["heap_size"] > 0, stats)
        check("map entries keep their declared value type",
              all(isinstance(k, str) for k in stats)
              and all(isinstance(v, int) for v in stats.values()), stats)
        check("the map agrees with the in-process call",
              set(stats) == set(fake_library.gc_stats()), sorted(stats))
        threw = None
        try:
            await client.call_function("gc_release_thread")
        except TypeError as e:
            threw = str(e)
        check("a function with no RPC surface says why",
              threw is not None and "threading policy" in threw, threw)
        threw = None
        try:
            await client.call_function("nope")
        except ValueError as e:
            threw = str(e)
        check("unknown free function fails typed", threw is not None, threw)

        # ---- external tool via reflection -------------------------------
        if not grpcurl_bin:
            print("[SKIP] grpcurl not found")
        else:
            rc, out, err = await run_tool(grpcurl_bin, symbol="list")
            if not out.strip():
                print(f"[WARN] grpcurl list empty; rc={rc} err={err!r}")
            for svc in ("Session", "LocalStoreService", "EvalStateService",
                        "ValueService", "DerivationService",
                        "FunctionsService", "StoreService"):
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

            # A free function through the external tool: the descriptor
            # name and the dispatch path must agree, which they did not
            # when the service was declared under its bare name.
            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.FunctionsService/collect_garbage",
                payload='{}')
            check("grpcurl calls a free function", rc == 0,
                  f"rc={rc} out={out[:80]!r} err={err[:120]!r}")

            # A map field, read by a tool that only has the descriptor
            # this build emitted. proto3 spells a map as a repeated
            # entry message, so a wrong synthesised entry type parses
            # in Python and still fails here.
            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.FunctionsService/gc_stats",
                payload='{}')
            counters: dict[str, Any] = (
                json.loads(out).get("result", {}) if rc == 0 else {})
            check("grpcurl reads a map return",
                  int(counters.get("heap_size", 0)) > 0,
                  f"rc={rc} out={out[:120]!r} err={err[:120]!r}")

            # Shared store methods live on StoreService, the one place
            # they are declared. An external tool calls a store without
            # knowing which implementation is behind the handle - which
            # is what the abstract base is for.
            rc, out, err = await run_tool(
                grpcurl_bin,
                symbol="nixmock.v1.StoreService/add_text_to_store",
                payload=json.dumps({
                    "self": {"id": handle},
                    "name": "via-grpcurl.txt",
                    "contents": "external tool"}))
            base = json.loads(out)["result"]["base_name"]
            check("grpcurl typed call returns StorePath",
                  base.endswith("via-grpcurl.txt"), base)

            # ...and the same rpc, same handle-shaped request, against a
            # RemoteStore. One service, either implementation.
            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.RemoteStoreService/Acquire",
                payload='{}')
            rhandle = json.loads(out)["id"]
            rc, out, err = await run_tool(
                grpcurl_bin, symbol="nixmock.v1.StoreService/get_uri",
                payload=json.dumps({"self": {"id": rhandle}}))
            check("one StoreService serves both implementations",
                  json.loads(out).get("result") == "uds://daemon", out[:120])

        print("\nALL REMOTE CHECKS PASSED")

        # aclose() is the shared way to let an object go: locally it
        # shuts the runner's thread down, remotely it hands the lease
        # back. Same call either side, which is what puts it on the
        # protocol.
        await store.aclose()
        check("aclose releases the lease remotely", store.handle_id is None)
        await client.release(rstore)
        await client.release(state)
    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), 5)
        except TimeoutError:
            server.kill()
        await drain_task
        if any("Application error" in line for line in logs):
            print("\n--- server reported application errors ---")
        print(f"\n--- server log ({len(logs)} lines, last 10) ---")
        for line in logs[-10:]:
            print(line.rstrip())


if __name__ == "__main__":
    asyncio.run(main())
