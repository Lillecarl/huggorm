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

import huggorm_bindings
import huggorm_generated.async_store
from huggorm_bindings import ContentAddressMethod as CA
from huggorm_bindings import HashAlgorithm
from huggorm_bindings.errors import BadStorePath
from huggorm_generated import RPCValue
from huggorm_generated._runtime import InternalError


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
    store = await client.acquire("Store", "dummy://")
    assert store.handle_id
    await store.aclose()
    # aclose is the shared way to let an object go: locally it shuts the
    # runner's thread down, remotely it hands the lease back. Same call
    # either side, which is what puts it on the protocol.
    assert store.handle_id is None, "aclose releases the lease remotely"


async def test_wire_value_arrives_as_a_local_object(
        client: Any, tmp_path: Any) -> None:
    store = await client.acquire("Store", str(tmp_path))
    with anyio.fail_after(10):
        p = await store.add_to_store("hello.txt", b"world")
    # A LOCAL object of the real bound class, not a handle: a wire
    # value crosses as its parts and is rebuilt on this side.
    assert type(p).__module__ == "huggorm_bindings.path", type(p).__name__
    assert p.to_string().endswith("hello.txt")
    with anyio.fail_after(10):
        assert await store.is_valid_path(p) is True
    await store.aclose()


async def test_a_value_argument_crosses_as_a_copy(
        client: Any, tmp_path: Any) -> None:
    store = await client.acquire("Store", str(tmp_path))
    held = await store.add_to_store("mysite", b"contents")
    # A wire value going the OTHER way: the StorePath was rebuilt
    # here, and passing it back encodes it as its parts again.
    assert await store.is_valid_path(held)
    assert (await store.query_path_info(held)).path() == held
    await store.aclose()


async def test_a_proxy_stays_remote(client: Any) -> None:
    state = await client.acquire("EvalState", "dummy://")
    v = await state.make_int(7)
    assert isinstance(v, RPCValue)
    assert v._wire == "proxy"
    # The generated class carries real methods, so a missing one is a
    # plain AttributeError from Python - not a manifest lookup that
    # produced a coroutine either way.
    from huggorm import remote


    assert not hasattr(remote, "RemoteObj")
    assert type(v).__module__ == "huggorm_generated.rpc"
    # It answers, which means the call landed on the state's own
    # thread: a Value is affine and every method on it is routed to
    # the runner the producing state owns.
    assert await v.integer() == 7
    await state.aclose()


async def test_backfilled_any_params_call_over_the_wire(client: Any) -> None:
    """attrs_set used to ship 'Any' params - alive locally, uncallable
    over the wire. The attribute count proves the call landed.

    Its second parameter is a Value, so the call carries a HANDLE
    argument as well as a string: the server resolves it back to the
    object before the method runs."""
    state = await client.acquire("EvalState", "dummy://")
    bag = await state.make_attrs()
    before = await bag.size()
    with anyio.fail_after(10):
        await state.attrs_set(bag, "wire_added", await state.make_int(1))
    assert await bag.size() == before + 1
    assert await (await bag.get("wire_added")).integer() == 1
    await state.aclose()


async def test_thunks_force_remotely(client: Any) -> None:
    state = await client.acquire("EvalState", "dummy://")
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
    state = await client.acquire("EvalState", "dummy://")
    with pytest.raises(InternalError) as caught:
        await state.parse_expr("")
    assert type(caught.value.__cause__) is ValueError
    assert caught.value.to_dict()["cause_type"] == "ValueError"
    await state.aclose()


async def test_a_nix_error_keeps_its_type_and_its_colour(client: Any) -> None:
    """A remote failure has the same shape as an in-process one.

    A declared error reaches the caller AS ITSELF, here as it does
    against the compiled binding. It used to be approximated by NAME
    against a map of five builtins, so a BadStorePath arrived as a
    plain Exception; then it crossed as its declared parts but under
    an InternalError, so `except BadStorePath` still caught nothing
    (tasks/036, tasks/066).

    The colour survives either way, which is the field that exists
    for the caller's terminal - and the caller with a terminal is
    usually the remote one."""
    store = await client.acquire("Store", "dummy://")
    with pytest.raises(BadStorePath) as caught:
        await store.parse_store_path("/somewhere/else/x")

    err = caught.value
    # ...the LEAF class, not a base it happens to derive from.
    assert type(err) is BadStorePath, type(err)
    assert "is not in the Nix store" in str(err)
    # The plain message stays plain, and the coloured one is what
    # libstore actually wrote.
    assert "\x1b[" not in str(err), repr(str(err))
    assert "\x1b[" in err.colored, repr(err.colored)
    await store.aclose()


async def test_an_undeclared_cause_still_approximates(client: Any) -> None:
    """Rebuilding is for what the manifest DECLARES, and nothing else.

    The evaluator raises std::invalid_argument, which the binding
    surfaces as a plain ValueError - not a nix error, so it carries no
    declared parts and comes back the way it always did. Failing to
    rebuild an error must never replace it with a different one."""
    state = await client.acquire("EvalState", "dummy://")
    with pytest.raises(InternalError) as caught:
        await state.parse_expr("")
    assert type(caught.value.__cause__) is ValueError
    assert caught.value.to_dict()["cause_type"] == "ValueError"
    await state.aclose()


async def test_a_released_handle_fails_typed(client: Any) -> None:
    tmp = await client.acquire("Store", "dummy://")
    ghost_id = tmp.handle_id
    await client.release(tmp)
    threw = await typed_failure(client.proxy("Store", ghost_id).get_uri())
    assert threw["cause_type"] == "KeyError", threw


async def test_an_unknown_handle_fails_typed(client: Any) -> None:
    threw = await typed_failure(
        client.proxy("Store", "0" * 32).get_uri())
    assert threw["cause_type"] == "KeyError", threw


# -- what cannot be acquired -----------------------------------------------

async def test_a_produced_class_refuses_remote_construction(
        client: Any) -> None:
    """A produced class has no constructor to construct with. It comes
    back from the method that makes it, and nowhere else."""
    with pytest.raises(ValueError, match="PathInfo"):
        await client.acquire("PathInfo")


# -- free functions --------------------------------------------------------

async def test_a_free_function_crosses(client: Any) -> None:
    """No handle: a module-level function has no instance."""
    assert await client.call_function("collect_garbage") is None


# Two properties lost their only exercise when the store mock went,
# and both come back with the libexpr EvalState (tasks/060):
#
# - a FREE FUNCTION taking a bound handle. No real free function this
#   repo binds takes one. nix::copyPaths is the obvious first;
# - an UNTOUCHED AFFINE handle resolved as an argument. The server
#   resolves a handle to a wrapper whose affine target may not be
#   built yet, and that used to refuse. Every other handle here has
#   been called already, which is exactly what hid it the first time.
#
# `EvalState(store)` restores both at once: it is a factory taking a
# bound Store handle, and the state it makes is affine.


async def test_a_proxy_argument_resolves_against_another_object(
        client: Any) -> None:
    state = await client.acquire("EvalState", "dummy://")
    other = await client.acquire("EvalState", "dummy://")
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
    assert set(stats) == set(huggorm_bindings.gc_stats()), sorted(stats)


async def test_a_list_return_crosses_as_a_repeated_field(
        client: Any, tmp_path: Any) -> None:
    """A repeated field, which is the other container proto3 gives.

    Every element is a wire VALUE and crosses as its own message: a
    list of proxies is refused, because one lease per element is not
    something anything grants in bulk.

    Order is the difference from a map: a repeated field has one, and
    the client hands back what the server sent. It is the store's
    order, not a helpful one - libstore keeps these in a
    nix::StorePathSet, so the sequence is by HASH and a caller who
    expects it by name is wrong."""
    names = ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]

    # Ground truth from the binding itself, in this process. Comparing
    # one wire answer against another would prove nothing about order:
    # both would come back through the same decode, sorted or not.
    direct = huggorm_bindings.Store(str(tmp_path / "ground"))
    for name in names:
        direct.add_to_store(name, name.encode())
    expected = [p.to_string() for p in direct.query_all_valid_paths()]

    store = await client.acquire("Store", str(tmp_path / "wire"))
    assert await store.query_all_valid_paths() == []
    for name in names:
        await store.add_to_store(name, name.encode())
    paths = await store.query_all_valid_paths()

    # Real local objects rebuilt from their parts, not handles.
    assert all(type(p).__module__ == "huggorm_bindings.path"
               for p in paths), [type(p) for p in paths]
    assert [p.to_string() for p in paths] == expected
    # ...and that order is not the one by NAME, which is what makes a
    # client that imposed its own ordering observable at all.
    assert [p.name() for p in paths] != sorted(names), paths
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
    local = huggorm_bindings.Store(str(tmp_path))
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

    local = huggorm_bindings.Store(str(tmp_path))
    assert path.to_string() == local.add_to_store(
        "greeting", b"hello world\n",
        CA.NAR, HashAlgorithm.SHA256).to_string()
    await store.aclose()


async def test_a_path_is_read_on_the_store_side(
        client: Any, tmp_path: Any) -> None:
    """add_path_to_store names a file the STORE reads, not one the
    caller sends.

    The string crosses and libstore opens it on the far side. Here
    both sides share a filesystem, so what this can assert is that the
    path really was read and hashed rather than treated as bytes: the
    remote answer matches a LOCAL add of the same directory, and a
    directory has no bytes form at all.

    `add_to_store` is the call that carries contents across. This one
    is for a path the store can already reach - which is exactly what
    a daemon-side build input is."""
    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello\n")
    (src / "sub" / "b.txt").write_text("world\n")

    store = await client.acquire("Store", str(tmp_path / "store"))
    path = await store.add_path_to_store("tree", str(src))

    local = huggorm_bindings.Store(str(tmp_path / "store"))
    assert path.to_string() == local.add_path_to_store(
        "tree", str(src)).to_string()
    assert await store.is_valid_path(path)
    await store.aclose()


async def test_a_path_info_crosses_as_a_value(
        client: Any, tmp_path: Any) -> None:
    """A PathInfo is what the store SAID, so it crosses as a copy.

    No handle, no lease, no second round trip: a caller reads every
    field off a real local object. That is the whole point of the
    value policy, and it is why this type has no async form on either
    side.

    `deriver` is the sharp part. It is an optional nested VALUE, and a
    protobuf message field has real presence - so an absent one must
    read back as None rather than as a StorePath rebuilt from an empty
    base name, which raises."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    store = await client.acquire("Store", str(tmp_path / "store"))
    path = await store.add_path_to_store("tree", str(src))
    info = await store.query_path_info(path)

    assert isinstance(info, huggorm_bindings.PathInfo)
    assert info.path().to_string() == path.to_string()
    assert info.deriver() is None

    # A container field comes back as a LIST, not as the protobuf
    # container it travelled in. A repeated field cannot be assigned
    # either, so both directions go through the list helpers the rpc
    # layer already had - and an empty one proves the encoding half on
    # its own, because assigning even [] to a repeated field raises.
    # An optional SCALAR field, across the wire, on the arm that has
    # a value. proto3 gives it presence through a synthetic oneof, so
    # this is exact rather than a guess from truthiness (tasks/048).
    # ...and it is a MESSAGE with a message inside it, so this is
    # also the first nested wire-value to cross: a ContentAddress
    # holding a Hash holding an algorithm and raw digest bytes.
    ca = info.ca()
    assert ca is not None and str(ca).startswith("fixed:")
    assert ca.hash().algorithm() == "sha256"
    assert len(ca.hash().digest()) == 32

    assert info.references() == []
    assert isinstance(info.references(), list)
    assert isinstance(info.sigs(), list)

    local = huggorm_bindings.Store(str(tmp_path / "store"))
    same = local.query_path_info(local.parse_store_path(
        local.print_store_path(path)))
    assert info.nar_hash() == same.nar_hash()
    assert info.nar_size() == same.nar_size()
    await store.aclose()


async def test_a_store_location_crosses_as_a_value(
        client: Any, tmp_path: Any) -> None:
    """to_store_path answers in the SERVER's terms, and the answer is
    a copy.

    The question is which store object holds a file, and only the
    store knows its own directory - so the split has to happen there.
    What comes back is a pair with no handle and no lease: a
    StorePath the caller can pass straight back, and the sub-path as
    the string it is."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    store = await client.acquire("Store", str(tmp_path / "store"))
    path = await store.add_path_to_store("tree", str(src))
    printed = await store.print_store_path(path)

    where = await store.to_store_path(f"{printed}/a.txt")
    assert isinstance(where, huggorm_bindings.StoreLocation)
    assert isinstance(where.path(), huggorm_bindings.StorePath)
    assert where.path().to_string() == path.to_string()
    assert where.sub_path() == "/a.txt"

    # Empty is a real answer and proto3 cannot tell it from absent.
    # The field carries no "?", so it reads back as "" rather than as
    # None.
    assert (await store.to_store_path(printed)).sub_path() == ""
    await store.aclose()


async def test_the_reference_graph_crosses_both_ways(
        client: Any, tmp_path: Any) -> None:
    """query_referrers over the wire, against the edge that made it.

    The same list machinery in both directions and at two levels: a
    repeated field of messages as a RETURN here, and as a wire-value's
    own field in query_path_info. Asserting them against each other is
    what proves they agree - one edge, read forwards and backwards."""
    store = await client.acquire("Store", str(tmp_path / "store"))
    target = await store.add_to_store("target", b"pointed at\n")
    holder = await store.add_to_store(
        "holder", b"points at a target\n", references=[target])

    referrers = await store.query_referrers(target)
    assert [r.to_string() for r in referrers] == [holder.to_string()]
    assert all(isinstance(r, huggorm_bindings.StorePath) for r in referrers)

    info = await store.query_path_info(holder)
    assert [r.to_string() for r in info.references()] == [target.to_string()]

    assert await store.query_referrers(holder) == []
    assert await store.query_valid_derivers(target) == []
    await store.aclose()


async def test_a_closure_crosses_as_a_set(
        client: Any, tmp_path: Any) -> None:
    """A list of wire values in BOTH directions of one call.

    Every other list so far crossed one way: `references` goes out as
    a parameter, `query_referrers` comes back as a return. This sends
    a repeated field of messages and reads one back, which is the
    shape a closure has and the last combination untested.

    The bool defaults ride along: `flip_direction` is written by every
    surface, so the short call and the spelled-out one have to mean
    the same thing here as they do in process."""
    store = await client.acquire("Store", str(tmp_path / "store"))
    a = await store.add_to_store("a", b"the end of the line\n")
    b = await store.add_to_store("b", b"points at a\n", references=[a])

    closure = await store.compute_fs_closure([b])
    assert {p.to_string() for p in closure} == {a.to_string(), b.to_string()}
    assert all(isinstance(p, huggorm_bindings.StorePath) for p in closure)

    # The short call and the spelled-out one, on the arm where the
    # default matters.
    assert {p.to_string() for p in
            await store.compute_fs_closure([b], False, False, False)} == {
        a.to_string(), b.to_string()}
    assert {p.to_string() for p in
            await store.compute_fs_closure([a], flip_direction=True)} == {
        a.to_string(), b.to_string()}

    assert {p.to_string() for p in await store.query_valid_paths([a, b])} == {
        a.to_string(), b.to_string()}
    await store.aclose()


async def test_absence_crosses_as_absence(
        client: Any, tmp_path: Any) -> None:
    """A `T | None` return, both arms, over the wire.

    Absence needs no new machinery and no new field: a protobuf
    message field HAS presence, so an unset one IS the None. That is
    the same bit `_wire_fields` reads with a trailing "?" one level
    down, said in the annotation instead - where a typechecker reads
    it too.

    Both arms are asserted here because only one of them is
    interesting: a missing None looks exactly like a default-built
    object, which for a StorePath means an empty base name that
    raises."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    store = await client.acquire("Store", str(tmp_path / "store"))
    path = await store.add_path_to_store("tree", str(src))
    hash_part = path.to_string().split("-", 1)[0]

    found = await store.query_path_from_hash_part(hash_part)
    assert found is not None
    assert found.to_string() == path.to_string()

    assert await store.query_path_from_hash_part("0" * 32) is None
    await store.aclose()


async def test_a_link_is_followed_on_the_store_side(
        client: Any, tmp_path: Any) -> None:
    """The symlinks are read where the STORE is, not where the caller
    is.

    That is the same meaning add_path_to_store already carries, and it
    is what makes this a remote call worth having: a client asking
    which store object a link points at is asking about the server's
    filesystem. The answer comes back in the store's terms, as a
    string, so it crosses as a scalar and needs no handle.

    real_path is the contrast: it names a location on the store's
    machine, which a client cannot reach, so it has no rpc at all."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_text("hello\n")

    store = await client.acquire("Store", str(tmp_path / "store"))
    path = await store.add_path_to_store("tree", str(src))
    printed = await store.print_store_path(path)

    link = tmp_path / "result"
    link.symlink_to(f"{printed}/a.txt")

    assert await store.follow_links_to_store(str(link)) == f"{printed}/a.txt"
    assert (await store.follow_links_to_store_path(
        str(link))).to_string() == path.to_string()

    # real_path is not merely refused here - it is ABSENT. A wire
    # blocker takes a method off the protocol, so the remote class
    # never grows it, while the in-process wrapper keeps it.
    assert not hasattr(store, "real_path")
    assert hasattr(huggorm_generated.async_store.AsyncStore, "real_path")
    await store.aclose()


async def test_a_reference_list_crosses_both_ways(
        client: Any, tmp_path: Any) -> None:
    """A container of wire values, in a parameter and in a return.

    This is the round trip an empty list cannot prove. Going out, a
    list of StorePath fills a repeated field of messages; coming back,
    the same field rebuilds real StorePath objects rather than the
    protobuf container they arrived in.

    The default proves the other half: `references` may be omitted,
    and absence crosses as a repeated field with nothing in it because
    that is the only thing it can be."""
    store = await client.acquire("Store", str(tmp_path / "store"))
    target = await store.add_to_store("target", b"pointed at\n")
    holder = await store.add_to_store(
        "holder", b"points at a target\n", references=[target])

    info = await store.query_path_info(holder)
    references = info.references()
    assert [r.to_string() for r in references] == [target.to_string()]
    assert all(isinstance(r, huggorm_bindings.StorePath) for r in references)

    assert (await store.query_path_info(target)).references() == []
    await store.aclose()


async def test_a_NESTED_value_crosses_as_an_argument(
        client: Any, tmp_path: Any) -> None:
    """`query_realisation` takes a DrvOutput, which holds a Hash.

    A wire value has crossed as an argument since `is_valid_path` took
    a StorePath, and bytes have crossed since `add_to_store` took a
    file's contents. What is new here is a message argument that
    itself CONTAINS a message: DrvOutput -> Hash -> raw digest bytes.
    The client encodes that nesting and the server decodes it, which
    is the direction a returned value never exercises.

    The answer is None, and that is the store's own: `ca-derivations`
    is off, so libstore consults no mapping.

    WHAT THIS DOES NOT PROVE, said plainly rather than implied: the
    answer does not depend on the digest, so a client that mangled it
    would still get None. Nothing bound today echoes a value back, so
    there is nothing to ask. The round-trip gate in test_store.py is
    where the codec's fidelity is proven; this proves the RPC layer
    builds and accepts the nested message at all, which is the part
    that gate cannot see."""
    store = await client.acquire("Store", str(tmp_path / "store"))
    key = huggorm_bindings.DrvOutput(
        huggorm_bindings.Hash(
            huggorm_bindings.HashAlgorithm.SHA256, bytes(range(32))),
        "out")

    assert await store.query_realisation(key) is None
    await store.aclose()


async def test_a_function_with_no_rpc_surface_says_why(client: Any) -> None:
    with pytest.raises(TypeError, match="threading policy"):
        await client.call_function("gc_release_thread")


async def test_an_unknown_free_function_fails_typed(client: Any) -> None:
    with pytest.raises(ValueError, match="nope"):
        await client.call_function("nope")
