"""
Handle-lifecycle test harness (tasks/002).

Starts a SHORT-TTL server and drives every lifetime path over real
gRPC: bind/claim, leases, producer pinning with cascading reaps,
share (copy|transfer), detach-to-escrow surviving connection death,
and TTL-based disconnect reaping. Reuses the scaffolding from
test_remote.

Run:  nix run --file . ourPython -- test_lifecycle.py
"""

import asyncio
import sys

from test_remote import check, free_port, wait_port

TTL = 3.0


async def spawn_server(port):
    server = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "fake_library_python.server",
        "127.0.0.1", str(port), str(TTL),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    await wait_port(port)
    return server


async def main():
    from fake_library_generated._runtime import InternalError
    from fake_library_python import remote

    port = free_port()
    server = await spawn_server(port)
    try:
        # ---- bind and basic lease semantics --------------------------
        a = await remote.connect("127.0.0.1", port)
        b = await remote.connect("127.0.0.1", port)
        check("distinct clients get distinct tokens", a.token != b.token)

        store_a = await a.acquire("LocalStore")
        check("acquire works", bool(store_a.handle_id))

        # Capability: anyone holding the ID may call; lifetime is what
        # tokens govern. b can use a's handle.
        cross = b.proxy("LocalStore", store_a.handle_id)
        uri = await cross.get_uri()
        check("handle usable across connections", uri == "local")

        # Release once, then release the SAME handle id through a fresh
        # object. Reusing store_a sent an empty id the second time (the
        # client blanks handle_id on success), so this used to assert
        # that releasing handle "" fails - which proves nothing.
        hid_a = store_a.handle_id
        await a.release(store_a)
        again = a.proxy("LocalStore", hid_a)
        threw = None
        try:
            await a.release(again)
        except InternalError as e:
            threw = e.to_dict()
        check("double release fails typed",
              threw is not None and threw["cause_type"] == "ValueError"
              and hid_a[:8] in threw["cause_message"], threw)

        threw = None
        try:
            await a.release(store_a)  # already blanked client-side
        except ValueError as e:
            threw = str(e)
        check("releasing a spent object fails client-side", threw is not None, threw)

        # ---- share: copy and transfer --------------------------------
        tok_b = b.token

        async def b_calls(hid):
            probe = b.proxy("LocalStore", hid)
            return await probe.get_uri()

        # a's first lease was consumed by the successful release above.
        # Re-acquire cleanly; capture ids before releasing (release()
        # clears the client-side handle).
        store_a2 = await a.acquire("LocalStore")
        hid_a2 = store_a2.handle_id
        await a.share(store_a2, tok_b, mode="copy")
        await a.release(store_a2)
        check("copied lease survives granter's release",
              await b_calls(hid_a2) == "local")

        store_x = await a.acquire("LocalStore")
        hid_x = store_x.handle_id
        await a.share(store_x, tok_b, mode="transfer")
        threw = None
        try:
            await a.release(store_x)
        except InternalError as e:
            threw = e.to_dict()
        check("transfer moves ownership: granter cannot release",
              threw is not None and threw["cause_type"] == "ValueError", threw)
        check("transferee holds the lease", await b_calls(hid_x) == "local")

        # ---- producer pinning and cascade reap -----------------------
        rstore = await a.acquire("RemoteStore")
        drv_path = await rstore.add_text_to_store("life.drv", "DrvLife")
        drv = await rstore.query_derivation(drv_path)
        desc = await drv.describe()
        check("proxy produced and pinned", "seen 1x" in desc, desc)

        await a.release(rstore)
        # Producer entry survives: its child drv pins it. Calls still
        # work (the token governs lifetime, the ID remains access).
        desc2 = await drv.describe()
        check("child keeps producer alive after producer release",
              "seen 2x" in desc2, desc2)

        hid_drv = drv.handle_id
        await a.release(drv)
        # Now the graph is unreferenced: both entries drop together.
        phantom_drv = a.proxy("Derivation", hid_drv)
        threw = None
        try:
            await phantom_drv.describe()
        except InternalError as e:
            threw = e.to_dict()
        check("cascade drops parent and child",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        # ---- detach to escrow, claim from a later connection ---------
        state = await a.acquire("EvalState", "local")
        thunk = await state.parse_expr("42")
        await state.force(thunk)
        hid_thunk = thunk.handle_id
        detached = await a.detach(all=True)
        check("detach reports moved leases", detached)
        # a no longer holds them: release must fail...
        threw = None
        try:
            await a.release(thunk)
        except InternalError as e:
            threw = e.to_dict()
        check("detached leases are no longer ours",
              threw is not None and threw["cause_type"] == "ValueError", threw)
        # ...but the objects stay alive, ownerless.
        check("detached handles remain callable",
              await thunk.integer() == 42)

        a.stop_pinging()
        # Abandon a entirely; let the sweeper reap everything it held.
        await asyncio.sleep(TTL * 1.5 + 1.0)

        c = await remote.connect("127.0.0.1", port, claim=a.token)
        check("claim adopts the detached token", c.token == a.token)
        claimed_thunk = c.proxy("Value", thunk.handle_id)
        check("escrowed objects survive connection death",
              await claimed_thunk.integer() == 42)

        # A claimed lease must still be a NORMAL lease: releasing it
        # drops the handle. Regression guard for the escrow double
        # count, where Bind added a lease instead of moving the escrowed
        # one back, so no number of Releases ever reached zero and every
        # detach/claim round trip leaked its handle for good.
        await c.release(claimed_thunk)
        phantom_claimed = c.proxy("Value", hid_thunk)
        threw = None
        try:
            await phantom_claimed.integer()
        except InternalError as e:
            threw = e.to_dict()
        check("releasing a claimed lease drops the handle",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        # ---- TTL reaping of abandoned (non-detached) handles ---------
        d = await remote.connect("127.0.0.1", port)
        doomed = await d.acquire("LocalStore")
        d.stop_pinging()
        await asyncio.sleep(TTL * 1.5 + 1.0)
        phantom = c.proxy("LocalStore", doomed.handle_id)
        threw = None
        try:
            await phantom.get_uri()
        except InternalError as e:
            threw = e.to_dict()
        check("abandoned connection's handles are reaped after ttl",
              threw is not None and threw["cause_type"] == "KeyError", threw)

        # Pinging connections are immune (control).
        alive = await c.acquire("LocalStore")
        await asyncio.sleep(TTL * 1.5 + 1.0)
        check("pinging client survives the sweeper",
              await alive.get_uri() == "local")

        # ---- a dropped client object releases its lease (tasks/028) --
        # Which is exactly why the control above needs care: a pinging
        # connection is immune to the sweeper, so before this the only
        # way it ever gave a handle back was an explicit release. Every
        # proxy it was ever granted stayed alive for the life of the
        # connection.
        import gc

        d = await remote.connect("127.0.0.1", port)
        state = await d.acquire("EvalState", "local")
        ids = []
        for i in range(10):
            v = await state.eval_expr(f'"v{i}"')
            ids.append(v.handle_id)
            del v
        gc.collect()
        check("dropping the last reference queues a release",
              len(d._dropped) == 10, d._dropped)
        check("flush releases the whole batch in one rpc",
              await d.flush_dropped() == 10)
        still = 0
        for hid in ids:
            try:
                await d.proxy("Value", hid).string_value()
                still += 1
            except InternalError:
                pass
        check("dropped handles are gone server-side", still == 0, still)

        # Two client objects, ONE handle: the first drop must not pull
        # the lease out from under the second. Forging a proxy from a
        # raw id is public API, and this file does it seven times above.
        v = await state.eval_expr('"shared"')
        shared_id = v.handle_id
        twin = d.proxy("Value", shared_id)
        del v
        gc.collect()
        await d.flush_dropped()
        check("one of two holders dropped: handle survives",
              await twin.string_value() == "shared")
        del twin
        gc.collect()
        await d.flush_dropped()
        threw = None
        try:
            await d.proxy("Value", shared_id).string_value()
        except InternalError as e:
            threw = e.to_dict()
        check("last holder dropped: handle released", threw is not None)

        # Queued, then used again before the flush. Releasing it there
        # would take the lease from a live object.
        v = await state.eval_expr('"resurrected"')
        res_id = v.handle_id
        del v
        gc.collect()
        again = d.proxy("Value", res_id)
        check("re-acquired before the flush is not released",
              await d.flush_dropped() == 0 and
              await again.string_value() == "resurrected")
        await again.aclose()

        # An explicitly closed handle must not be queued a second time
        # when its object is later collected.
        v = await state.eval_expr('"closed"')
        await v.aclose()
        del v
        gc.collect()
        check("explicit close does not queue a second release",
              d._dropped == [], d._dropped)
        await state.aclose()
        d.stop_pinging()

        for cli in (b, c, d):
            cli.stop_pinging()
        print("\nALL LIFECYCLE CHECKS PASSED")
    finally:
        server.terminate()
        try:
            await asyncio.wait_for(server.wait(), 5)
        except asyncio.TimeoutError:
            server.kill()


if __name__ == "__main__":
    asyncio.run(main())
