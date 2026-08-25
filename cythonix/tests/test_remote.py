"""
The remote layer over a real gRPC socket: the proxy/value matrix.

A wire-value crosses as a copy and arrives as a real local object; a
proxy stays remote behind a handle. Both directions, as arguments and
as returns, plus the error fidelity that makes a failure debuggable
rather than anonymous.
"""

from typing import Any

import anyio
import pytest

import cythonix_bindings
from cythonix_bindings import DerivedPath
from cythonix_generated import RPC_CLASSES, RPCDerivation
from cythonix_generated._runtime import InternalError


async def typed_failure(coro: Any) -> dict[str, str]:
    try:
        await coro
    except InternalError as e:
        return e.to_dict()
    raise AssertionError("expected the call to fail")


# -- values and proxies ----------------------------------------------------

async def test_acquire_returns_a_handle(client: Any) -> None:
    store = await client.acquire("LocalStore")
    assert store.handle_id
    await store.aclose()
    # aclose is the shared way to let an object go: locally it shuts the
    # runner's thread down, remotely it hands the lease back. Same call
    # either side, which is what puts it on the protocol.
    assert store.handle_id is None, "aclose releases the lease remotely"


async def test_wire_value_arrives_as_a_local_object(client: Any) -> None:
    store = await client.acquire("LocalStore")
    with anyio.fail_after(10):
        p = await store.add_text_to_store("hello.txt", "world")
    assert type(p).__module__ == "cythonix_bindings.store", type(p).__name__
    assert p.to_string().endswith("hello.txt")
    with anyio.fail_after(10):
        assert await store.is_valid_path(p) is True
    await store.aclose()


async def test_a_value_argument_crosses_as_a_copy(client: Any) -> None:
    store = await client.acquire("LocalStore")
    drv_path = await store.add_text_to_store("mysite.drv", "DrvMine")
    built = await store.build_derivation(DerivedPath(drv_path, "out"))
    assert built.to_string().endswith("-out")
    assert await store.is_valid_path(built)
    await store.aclose()


async def test_a_proxy_stays_remote(client: Any) -> None:
    rstore = await client.acquire("RemoteStore")
    drv = await rstore.query_derivation(
        await rstore.add_text_to_store("demo.drv", "DrvDemo"))
    assert isinstance(drv, RPCDerivation)
    assert drv._wire == "proxy"
    # The generated class carries real methods, so a missing one is a
    # plain AttributeError from Python - not a manifest lookup that
    # produced a coroutine either way.
    from cythonix import remote


    assert not hasattr(remote, "RemoteObj")
    assert type(drv).__module__ == "cythonix_generated.rpc"
    assert "seen 1x" in await drv.describe(), "runs on the producer's thread"
    await rstore.aclose()


async def test_backfilled_any_params_call_over_the_wire(client: Any) -> None:
    """set_env used to ship 'Any' params - alive locally, uncallable
    over the wire. The env-count delta proves the call landed."""
    rstore = await client.acquire("RemoteStore")
    drv = await rstore.query_derivation(
        await rstore.add_text_to_store("env.drv", "DrvEnv"))

    def env_count(d: str) -> int:
        return int(d.split("(")[1].split()[0])

    before = await drv.describe()
    with anyio.fail_after(10):
        await drv.set_env("wire_added", "1")
    after = await drv.describe()
    assert env_count(after) == env_count(before) + 1, f"{before!r} -> {after!r}"
    await rstore.aclose()


async def test_thunks_force_remotely(client: Any) -> None:
    state = await client.acquire("EvalState", "local")
    thunk = await state.parse_expr("42")
    await typed_failure(thunk.integer())
    await state.force(thunk)
    assert await thunk.integer() == 42, "force mutates in place remotely"

    v = await state.eval_expr('"hello over grpc"')
    assert await v.string_value() == "hello over grpc"
    # bint-returning methods cross as real booleans. 'bint' used to leak
    # into the schema and map to an opaque Handle, killing the rpc.
    with anyio.fail_after(10):
        assert await v.is_gc_managed() is True
    await state.aclose()


# -- error fidelity --------------------------------------------------------

async def test_a_decoded_cause_survives_the_wire(client: Any) -> None:
    """A C++ failure crosses as a rebuilt InternalError whose cause
    survives: __cause__ must be the original ValueError, not None -
    the regression guard for `raise ... from None`."""
    rstore = await client.acquire("RemoteStore")
    bad_path = await rstore.add_text_to_store("plain.txt", "x")
    with pytest.raises(InternalError) as caught:
        await rstore.query_derivation(bad_path)
    assert type(caught.value.__cause__) is ValueError
    assert caught.value.to_dict()["cause_type"] == "ValueError"
    await rstore.aclose()


async def test_a_released_handle_fails_typed(client: Any) -> None:
    tmp = await client.acquire("LocalStore")
    ghost_id = tmp.handle_id
    await client.release(tmp)
    threw = await typed_failure(client.proxy("LocalStore", ghost_id).get_uri())
    assert threw["cause_type"] == "KeyError", threw


async def test_an_unknown_handle_fails_typed(client: Any) -> None:
    threw = await typed_failure(
        client.proxy("LocalStore", "0" * 32).get_uri())
    assert threw["cause_type"] == "KeyError", threw


# -- the abstract base -----------------------------------------------------

@pytest.mark.parametrize(("kind", "uri"),
                         [("LocalStore", "local"),
                          ("RemoteStore", "uds://daemon")])
async def test_one_service_serves_either_implementation(
        client: Any, kind: str, uri: str) -> None:
    """A handle is a handle: the shared surface resolves through
    StoreService whichever implementation is behind it, and the Python
    client walks to the base exactly as Python would."""
    h = await client.acquire(kind)
    assert await h.get_uri() == uri
    path = type(h)._rpc["get_uri"]["rpc"]["path"]
    assert path == "/nixmock.v1.StoreService/get_uri", path
    assert isinstance(h, RPC_CLASSES["Store"]), type(h).__name__
    await client.release(h)


async def test_an_unguaranteed_method_is_absent(client: Any) -> None:
    """query_derivation is not guaranteed: the pool policy drops it
    from LocalStore, so it lives on RemoteStore alone. It is simply not
    on the class, so Python raises before any call is made."""
    pool_store = await client.acquire("LocalStore")
    with pytest.raises(AttributeError, match="query_derivation"):
        _ = pool_store.query_derivation
    assert hasattr(RPC_CLASSES["RemoteStore"], "query_derivation")
    await client.release(pool_store)


async def test_a_wire_value_refuses_remote_construction(client: Any) -> None:
    """There is no handle to construct into. It is built locally and
    passed as an argument."""
    with pytest.raises(ValueError, match="DerivedPath"):
        await client.acquire("DerivedPath")


# -- free functions --------------------------------------------------------

async def test_a_free_function_crosses(client: Any) -> None:
    """No handle: a module-level function has no instance."""
    assert await client.call_function("collect_garbage") is None


async def test_a_free_function_takes_a_base_handle(client: Any) -> None:
    """describe takes a Store. It had no RPC surface at all until Store
    became a generated base with a wire identity."""
    store = await client.acquire("LocalStore")
    rstore = await client.acquire("RemoteStore")
    assert await client.call_function("describe", store) == "store(local)"
    assert await client.call_function("describe", rstore) == "store(uds://daemon)"
    await store.aclose()
    await rstore.aclose()


async def test_an_untouched_affine_handle_resolves_as_an_argument(
        client: Any) -> None:
    """A handle is resolvable from the moment it exists. The server
    resolves it to a wrapper whose affine target may not be built yet;
    that used to refuse, so passing an untouched RemoteStore anywhere
    failed. Every other handle here had been called already, which hid
    it."""
    untouched = await client.acquire("RemoteStore")
    assert await client.call_function("describe", untouched) \
        == "store(uds://daemon)"
    await untouched.aclose()


async def test_a_proxy_argument_resolves_against_another_object(
        client: Any) -> None:
    state = await client.acquire("EvalState", "local")
    other = await client.acquire("EvalState", "local")
    loose = await state.parse_expr("7")
    await other.force(loose)
    assert await loose.integer() == 7
    await other.aclose()
    await state.aclose()


async def test_a_dict_return_crosses_as_a_map(client: Any) -> None:
    """Nix attribute names are always strings, so map<string, V> covers
    every dict this API returns; the value type comes from the
    declaration, which is the same annotation the typechecker reads
    (tasks/030)."""
    stats = await client.call_function("gc_stats")
    assert isinstance(stats, dict) and stats["heap_size"] > 0, stats
    assert all(isinstance(k, str) for k in stats), stats
    assert all(isinstance(v, int) for v in stats.values()), stats
    assert set(stats) == set(cythonix_bindings.gc_stats()), sorted(stats)


async def test_a_function_with_no_rpc_surface_says_why(client: Any) -> None:
    with pytest.raises(TypeError, match="threading policy"):
        await client.call_function("gc_release_thread")


async def test_an_unknown_free_function_fails_typed(client: Any) -> None:
    with pytest.raises(ValueError, match="nope"):
        await client.call_function("nope")
