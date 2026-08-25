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
from cythonix_bindings import ContentAddressMethod as CA
from cythonix_bindings import HashAlgorithm, MockDerivedPath
from cythonix_bindings.errors import BadStorePath
from cythonix_generated import RPC_CLASSES, RPCMockDerivation
from cythonix_generated._runtime import InternalError


async def typed_failure(coro: Any) -> dict[str, str]:
    """What a failed call said about itself: the wrapper's own fields
    plus the approximated cause. The typed cause, when there is one,
    is on __cause__ instead - see the error-fidelity tests."""
    try:
        await coro
    except InternalError as e:
        return e.to_dict()
    raise AssertionError("expected the call to fail")


# -- values and proxies ----------------------------------------------------

async def test_acquire_returns_a_handle(client: Any) -> None:
    store = await client.acquire("MockLocalStore")
    assert store.handle_id
    await store.aclose()
    # aclose is the shared way to let an object go: locally it shuts the
    # runner's thread down, remotely it hands the lease back. Same call
    # either side, which is what puts it on the protocol.
    assert store.handle_id is None, "aclose releases the lease remotely"


async def test_wire_value_arrives_as_a_local_object(client: Any) -> None:
    store = await client.acquire("MockLocalStore")
    with anyio.fail_after(10):
        p = await store.add_text_to_store("hello.txt", "world")
    assert type(p).__module__ == "cythonix_bindings.mock_store", type(p).__name__
    assert p.to_string().endswith("hello.txt")
    with anyio.fail_after(10):
        assert await store.is_valid_path(p) is True
    await store.aclose()


async def test_a_value_argument_crosses_as_a_copy(client: Any) -> None:
    store = await client.acquire("MockLocalStore")
    drv_path = await store.add_text_to_store("mysite.drv", "DrvMine")
    built = await store.build_derivation(MockDerivedPath(drv_path, "out"))
    assert built.to_string().endswith("-out")
    assert await store.is_valid_path(built)
    await store.aclose()


async def test_a_proxy_stays_remote(client: Any) -> None:
    rstore = await client.acquire("MockRemoteStore")
    drv = await rstore.query_derivation(
        await rstore.add_text_to_store("demo.drv", "DrvDemo"))
    assert isinstance(drv, RPCMockDerivation)
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
    rstore = await client.acquire("MockRemoteStore")
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
    rstore = await client.acquire("MockRemoteStore")
    bad_path = await rstore.add_text_to_store("plain.txt", "x")
    with pytest.raises(InternalError) as caught:
        await rstore.query_derivation(bad_path)
    assert type(caught.value.__cause__) is ValueError
    assert caught.value.to_dict()["cause_type"] == "ValueError"
    await rstore.aclose()


async def test_a_nix_error_keeps_its_type_and_its_colour(client: Any) -> None:
    """A remote failure has the same shape as an in-process one.

    In process, a binding failure is an InternalError whose __cause__
    is the real error. Over the wire the cause used to be approximated
    by NAME against a map of five builtins, so a BadStorePath arrived
    as a plain Exception and `except BadStorePath` caught nothing. The
    colour was gone entirely, which is the field that exists for the
    caller's terminal - and the caller with a terminal is usually the
    remote one (tasks/036)."""
    store = await client.acquire("Store", "dummy://")
    with pytest.raises(InternalError) as caught:
        await store.parse_store_path("/somewhere/else/x")

    cause = caught.value.__cause__
    assert isinstance(cause, BadStorePath), type(cause)
    # ...the LEAF class, not a base it happens to derive from.
    assert type(cause) is BadStorePath, type(cause)
    assert "is not in the Nix store" in str(cause)
    # The plain message stays plain, and the coloured one is what
    # libstore actually wrote.
    assert "\x1b[" not in str(cause), repr(str(cause))
    assert "\x1b[" in cause.colored, repr(cause.colored)
    await store.aclose()


async def test_an_undeclared_cause_still_approximates(client: Any) -> None:
    """Rebuilding is for what the manifest DECLARES, and nothing else.

    The mock raises std::invalid_argument, which the binding surfaces
    as a plain ValueError - not a nix error, so it carries no declared
    parts and comes back the way it always did. Failing to rebuild an
    error must never replace it with a different one."""
    rstore = await client.acquire("MockRemoteStore")
    bad_path = await rstore.add_text_to_store("plain.txt", "x")
    with pytest.raises(InternalError) as caught:
        await rstore.query_derivation(bad_path)
    assert type(caught.value.__cause__) is ValueError
    assert caught.value.to_dict()["cause_type"] == "ValueError"
    await rstore.aclose()


async def test_a_released_handle_fails_typed(client: Any) -> None:
    tmp = await client.acquire("MockLocalStore")
    ghost_id = tmp.handle_id
    await client.release(tmp)
    threw = await typed_failure(client.proxy("MockLocalStore", ghost_id).get_uri())
    assert threw["cause_type"] == "KeyError", threw


async def test_an_unknown_handle_fails_typed(client: Any) -> None:
    threw = await typed_failure(
        client.proxy("MockLocalStore", "0" * 32).get_uri())
    assert threw["cause_type"] == "KeyError", threw


# -- the abstract base -----------------------------------------------------

@pytest.mark.parametrize(("kind", "uri"),
                         [("MockLocalStore", "local"),
                          ("MockRemoteStore", "uds://daemon")])
async def test_one_service_serves_either_implementation(
        client: Any, kind: str, uri: str) -> None:
    """A handle is a handle: the shared surface resolves through
    StoreService whichever implementation is behind it, and the Python
    client walks to the base exactly as Python would."""
    h = await client.acquire(kind)
    assert await h.get_uri() == uri
    path = type(h)._rpc["get_uri"]["rpc"]["path"]
    assert path == "/nixmock.v1.MockStoreService/get_uri", path
    assert isinstance(h, RPC_CLASSES["MockStore"]), type(h).__name__
    await client.release(h)


async def test_an_unguaranteed_method_is_absent(client: Any) -> None:
    """query_derivation is not guaranteed: the pool policy drops it
    from MockLocalStore, so it lives on MockRemoteStore alone. It is simply not
    on the class, so Python raises before any call is made."""
    pool_store = await client.acquire("MockLocalStore")
    with pytest.raises(AttributeError, match="query_derivation"):
        _ = pool_store.query_derivation
    assert hasattr(RPC_CLASSES["MockRemoteStore"], "query_derivation")
    await client.release(pool_store)


async def test_a_wire_value_refuses_remote_construction(client: Any) -> None:
    """There is no handle to construct into. It is built locally and
    passed as an argument."""
    with pytest.raises(ValueError, match="MockDerivedPath"):
        await client.acquire("MockDerivedPath")


# -- free functions --------------------------------------------------------

async def test_a_free_function_crosses(client: Any) -> None:
    """No handle: a module-level function has no instance."""
    assert await client.call_function("collect_garbage") is None


async def test_a_free_function_takes_a_base_handle(client: Any) -> None:
    """describe takes a MockStore. It had no RPC surface at all until MockStore
    became a generated base with a wire identity."""
    store = await client.acquire("MockLocalStore")
    rstore = await client.acquire("MockRemoteStore")
    assert await client.call_function("describe", store) == "store(local)"
    assert await client.call_function("describe", rstore) == "store(uds://daemon)"
    await store.aclose()
    await rstore.aclose()


async def test_an_untouched_affine_handle_resolves_as_an_argument(
        client: Any) -> None:
    """A handle is resolvable from the moment it exists. The server
    resolves it to a wrapper whose affine target may not be built yet;
    that used to refuse, so passing an untouched MockRemoteStore anywhere
    failed. Every other handle here had been called already, which hid
    it."""
    untouched = await client.acquire("MockRemoteStore")
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


async def test_a_list_return_crosses_as_a_repeated_field(client: Any) -> None:
    """A repeated field, which is the other container proto3 gives.

    Every element is a wire VALUE and crosses as its own message: a
    list of proxies is refused, because one lease per element is not
    something anything grants in bulk.

    Order is the difference from a map: a repeated field has one, and
    the client hands back what the server sent. It is the store's
    order, not a helpful one - the mock keeps base names in a
    std::set, exactly as nix::StorePathSet does, so the sequence is by
    HASH and a caller who expects it by name is wrong."""
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]

    # Ground truth from the binding itself, in this process. Comparing
    # one wire answer against another would prove nothing about order:
    # both would come back through the same decode, sorted or not.
    direct = cythonix_bindings.MockLocalStore()
    for name in names:
        direct.add_text_to_store(name, name)
    expected = [p.to_string() for p in direct.query_all_valid_paths()]

    store = await client.acquire("MockLocalStore")
    assert await store.query_all_valid_paths() == []
    for name in names:
        await store.add_text_to_store(name, name)
    paths = await store.query_all_valid_paths()

    # Real local objects rebuilt from their parts, not handles.
    assert all(type(p).__module__ == "cythonix_bindings.mock_store"
               for p in paths), [type(p) for p in paths]
    assert [p.to_string() for p in paths] == expected
    # ...and that order is not the one by NAME, which is what makes a
    # client that imposed its own ordering observable at all.
    assert [p.name_part() for p in paths] != sorted(names), paths
    await store.aclose()


async def test_bytes_cross_as_bytes(client: Any, tmp_path: Any) -> None:
    """File contents are not text and must not be encoded as if they
    were.

    A str field would round-trip a NAR into mojibake, and the hash
    that names the store path would be a hash of the wrong thing. So
    the schema gives `data` a protobuf `bytes` field, and the proof is
    a payload that is not valid utf-8 arriving byte for byte - which
    it does only if nothing tried to decode it on the way."""
    store = await client.acquire("Store", str(tmp_path))
    blob = bytes(range(256))
    path = await store.add_to_store(
        "blob", blob, CA.FLAT, HashAlgorithm.SHA256)

    # The name is the hash of exactly those bytes, so computing the
    # same path locally is what proves they survived. Nothing here
    # compares the payload to itself.
    local = cythonix_bindings.Store(str(tmp_path))
    assert path.to_string() == local.add_to_store(
        "blob", blob, CA.FLAT, HashAlgorithm.SHA256).to_string()
    assert await store.is_valid_path(path)
    await store.aclose()


async def test_a_default_means_the_same_thing_remotely(
        client: Any, tmp_path: Any) -> None:
    """The short call answers the same on both sides of the socket.

    A default is a fact about the SIGNATURE, so the generated RPC
    client fills it in before the call leaves - and the request that
    crosses carries every argument. That is why the wire needs no way
    to say "absent": there is no absence by the time it gets there.

    Proven against a LOCAL call with nothing omitted, not against
    another remote one. Two remote calls that both dropped the
    arguments would agree with each other perfectly."""
    store = await client.acquire("Store", str(tmp_path))
    path = await store.add_to_store("greeting", b"hello world\n")

    local = cythonix_bindings.Store(str(tmp_path))
    assert path.to_string() == local.add_to_store(
        "greeting", b"hello world\n",
        CA.NAR, HashAlgorithm.SHA256).to_string()
    await store.aclose()


async def test_a_function_with_no_rpc_surface_says_why(client: Any) -> None:
    with pytest.raises(TypeError, match="threading policy"):
        await client.call_function("gc_release_thread")


async def test_an_unknown_free_function_fails_typed(client: Any) -> None:
    with pytest.raises(ValueError, match="nope"):
        await client.call_function("nope")
