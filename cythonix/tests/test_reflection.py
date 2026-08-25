"""
The same API from outside Python, through grpcurl and reflection.

grpcurl holds only the descriptor this build emitted, so it catches a
schema that is wrong in a way a Python round trip is happy with: a
service declared under one name and dispatched under another, a
synthesised map entry with the wrong shape, a recursive oneof built
wrong.
"""

import json
from typing import Any

import pytest
from conftest import Server, run_tool


@pytest.fixture(scope="session")
def pkg() -> str:
    return "nixmock.v1"


async def call(grpcurl: str, server: Server, symbol: str,
               payload: str | None = None) -> tuple[int | None, str, str]:
    return await run_tool(grpcurl, server.port, symbol, payload)


@pytest.mark.parametrize("svc", [
    "Session", "MockLocalStoreService", "EvalStateService", "ValueService",
    "MockDerivationService", "FunctionsService", "MockStoreService",
])
async def test_reflection_lists_every_service(
        grpcurl: str, server: Server, pkg: str, svc: str) -> None:
    rc, out, err = await call(grpcurl, server, "list")
    assert f"{pkg}.{svc}" in out, f"rc={rc} out={out[:200]!r} err={err[:200]!r}"


async def test_acquire_is_a_typed_rpc(grpcurl: str, server: Server,
                                      pkg: str) -> None:
    """Construction lives on the class's own service, so an external
    tool sees the constructor's parameters in reflection instead of a
    free-text class name."""
    _, out, _ = await call(grpcurl, server, f"{pkg}.MockLocalStoreService/Acquire",
                           "{}")
    assert len(json.loads(out)["id"]) == 32, out[:200]

    _, out, _ = await call(grpcurl, server, f"{pkg}.EvalStateService/Acquire",
                           '{"store_uri":"local"}')
    assert len(json.loads(out).get("id", "")) == 32, out[:200]


async def test_a_free_function_is_reachable(grpcurl: str, server: Server,
                                            pkg: str) -> None:
    """The descriptor name and the dispatch path must agree, which they
    did not when the service was declared under its bare name."""
    rc, out, err = await call(
        grpcurl, server, f"{pkg}.FunctionsService/collect_garbage", "{}")
    assert rc == 0, f"rc={rc} out={out[:120]!r} err={err[:200]!r}"


async def test_a_map_return_reads(grpcurl: str, server: Server,
                                  pkg: str) -> None:
    """proto3 spells a map as a repeated entry message, so a wrong
    synthesised entry type parses in Python and still fails here."""
    rc, out, err = await call(grpcurl, server, f"{pkg}.FunctionsService/gc_stats",
                              "{}")
    counters: dict[str, Any] = json.loads(out).get("result", {}) if rc == 0 else {}
    assert int(counters.get("heap_size", 0)) > 0, \
        f"rc={rc} out={out[:200]!r} err={err[:200]!r}"


async def test_one_service_serves_both_implementations(
        grpcurl: str, server: Server, pkg: str) -> None:
    """Shared store methods live on StoreService, the one place they
    are declared. An external tool calls a store without knowing which
    implementation is behind the handle."""
    _, out, _ = await call(grpcurl, server, f"{pkg}.MockLocalStoreService/Acquire", "{}")
    local = json.loads(out)["id"]
    _, out, _ = await call(
        grpcurl, server, f"{pkg}.MockStoreService/add_text_to_store",
        json.dumps({"self": {"id": local}, "name": "via-grpcurl.txt",
                    "contents": "external tool"}))
    base = json.loads(out)["result"]["base_name"]
    assert base.endswith("via-grpcurl.txt"), base

    _, out, _ = await call(grpcurl, server, f"{pkg}.MockRemoteStoreService/Acquire", "{}")
    remote_id = json.loads(out)["id"]
    _, out, _ = await call(grpcurl, server, f"{pkg}.MockStoreService/get_uri",
                           json.dumps({"self": {"id": remote_id}}))
    assert json.loads(out).get("result") == "uds://daemon", out[:200]


async def test_the_recursive_value_message_reads(
        grpcurl: str, server: Server, pkg: str) -> None:
    """A oneof holding a map of itself is the shape most likely to be
    built wrong, and a wrong one still round-trips inside Python."""
    _, out, _ = await call(grpcurl, server, f"{pkg}.EvalStateService/Acquire",
                           '{"store_uri":"local"}')
    ev = json.loads(out)["id"]

    async def ev_call(method: str, **fields: Any) -> Any:
        _rc, o, _e = await call(grpcurl, server,
                                f"{pkg}.EvalStateService/{method}",
                                json.dumps({"self": {"id": ev}, **fields}))
        return json.loads(o) if o.strip() else {}

    attrs = (await ev_call("make_attrs"))["result"]["id"]
    seven = (await ev_call("make_int", value=7))["result"]["id"]
    await ev_call("attrs_set", target={"id": attrs}, name="n", item={"id": seven})

    rc, out, err = await call(grpcurl, server, f"{pkg}.Session/Realize",
                              json.dumps({"handle": {"id": attrs}}))
    realized: dict[str, Any] = json.loads(out) if rc == 0 else {}
    entry = (realized.get("root", {}).get("attrs", {})
             .get("entries", {}).get("n", {}))
    assert entry.get("i") == "7", \
        f"rc={rc} out={out[:250]!r} err={err[:200]!r}"
