"""One operation, three surfaces, one answer.

The premise of this repo is that a declaration decides every surface,
so the surfaces cannot disagree. `smoke_test.py` holds them to each
other by SIGNATURE - same names, same parameter types, same returns -
and a signature is not a result. Two implementations can agree on
every annotation and still answer differently, and the places they
would are exactly the interesting ones: a wire value rebuilt from its
parts on the far side, a proxy adopted onto a runner, an exception
translated out of C++ and then across a status detail.

So this runs the SAME body against all three and compares answers:

- **sync** - `huggorm_bindings.Store`, the compiled binding itself
- **async** - `huggorm_generated.AsyncStore`, the in-process wrapper
- **rpc** - `huggorm_generated.RPCStore`, over a real gRPC connection

Every test here is written once and parameterised, because a parity
test written three times is three tests that can drift.

Hermetic. `dummy://` is an in-memory store with no daemon and no
directory, so this whole file runs inside the build sandbox - which
is the point: parity is the property most worth checking on every
build, and a check that needs the machine's real store is not.
"""

import contextlib
import inspect
from typing import Any

import pytest

# The store every surface opens. In-memory, so nothing here touches
# the machine.
URI = "dummy://"

# A well-formed store path name that no store holds. The hash is real
# enough to parse; what matters is that `is_valid_path` says no on all
# three surfaces rather than raising on one.
ABSENT = "7rjjfrn5w3z1kb2v9v0ilxmvmb2n5k1y-hello-2.12.1"

SURFACES = ("sync", "async", "rpc")


async def call(obj: Any, name: str, *args: Any) -> Any:
    """Invoke one method on whichever surface, and hand back its answer.

    The one accommodation this suite makes, and it is the one
    difference the design intends: the sync binding answers a value
    and the two generated surfaces answer an awaitable. Everything
    else is asserted to be identical.

    Written as "await it if it is awaitable" rather than as a table of
    which surface is which, so a fourth surface would need no edit
    here - and so a method that accidentally stopped being async on
    one surface would still be compared rather than skipped."""
    result = getattr(obj, name)(*args)
    return await result if inspect.isawaitable(result) else result


@pytest.fixture(params=SURFACES)
async def store(request: Any, client: Any) -> Any:
    """The same store, opened three ways.

    The rpc surface needs the session server, so every parameter takes
    the `client` fixture even though two of them ignore it. Requesting
    it conditionally would make the fixture's dependencies depend on
    its own parameter, which pytest resolves before the parameter
    exists."""
    if request.param == "sync":
        from huggorm_bindings import Store

        return Store(URI)
    if request.param == "async":
        from huggorm_generated import AsyncStore

        return AsyncStore(URI)
    return await client.acquire("Store", URI)


@pytest.fixture(params=SURFACES)
async def state(request: Any, client: Any) -> Any:
    """An evaluator, opened three ways.

    The companion to `store` and a different shape of problem: a Store
    is POOL-threaded and an EvalState is AFFINE, so the async and rpc
    surfaces pin it to one thread. A caller cannot tell, and this
    file is where that claim is checked rather than asserted."""
    if request.param == "sync":
        from huggorm_bindings import EvalState

        return EvalState(URI)
    if request.param == "async":
        from huggorm_generated import AsyncEvalState

        return AsyncEvalState(URI)
    return await client.acquire("EvalState", URI)


# -- scalars ----------------------------------------------------------
# The simplest thing that can differ: a str, a bool, an int. If these
# disagree the transport is wrong in a way nothing subtler will
# survive.


async def test_a_store_reports_the_uri_it_was_opened_with(
        store: Any) -> None:
    assert await call(store, "get_uri") == URI


async def test_an_absent_path_is_invalid_everywhere(store: Any) -> None:
    from huggorm_bindings import StorePath

    assert await call(store, "is_valid_path", StorePath(ABSENT)) is False


async def test_printing_a_path_gives_the_same_string(store: Any) -> None:
    from huggorm_bindings import StorePath

    printed = await call(store, "print_store_path", StorePath(ABSENT))
    assert printed == f"/nix/store/{ABSENT}"


# -- wire values ------------------------------------------------------
# A StorePath is a VALUE: it crosses as its declared parts and is
# rebuilt by `_from_parts` on the far side. So the rpc surface hands
# back a real local StorePath, and this asserts that the rebuilt one
# equals the one the other two never sent anywhere.


async def test_a_parsed_path_comes_back_equal_everywhere(
        store: Any) -> None:
    from huggorm_bindings import StorePath

    got = await call(store, "parse_store_path", f"/nix/store/{ABSENT}")
    assert isinstance(got, StorePath), type(got)
    assert got == StorePath(ABSENT)
    assert got.to_string() == ABSENT


async def test_a_value_that_crossed_still_compares_and_hashes(
        store: Any) -> None:
    """Not just equal to itself: usable as a value.

    A wire value declares an ordering and a hash, and the rebuilt one
    has to have them too - otherwise a set of paths deduplicates on
    one surface and not on another."""
    a = await call(store, "parse_store_path", f"/nix/store/{ABSENT}")
    b = await call(store, "parse_store_path", f"/nix/store/{ABSENT}")
    assert a == b
    assert len({a, b}) == 1
    assert repr(a) == repr(b)


# -- errors -----------------------------------------------------------
# The hardest thing to keep identical. Locally a C++ exception is
# translated into a Python one by the binding; remotely it is encoded
# into a status detail, sent, and rebuilt from its declared parts.


def surface_of(obj: Any) -> str:
    """Which surface an object came from, by its class name.

    The three surfaces are exactly three shapes - `Store`,
    `AsyncStore`, `RPCStore` - so this needs no table and cannot fall
    out of step with one."""
    name = type(obj).__name__
    if name.startswith("Async"):
        return "async"
    if name.startswith("RPC"):
        return "rpc"
    return "sync"


@contextlib.contextmanager
def declared_error(kind: type[BaseException], obj: Any) -> Any:
    """Assert a DECLARED error reaches the caller as itself.

    It does on the sync surface and does not on the other two, and
    that is a parity defect rather than a fact of the design.
    `StoreLike` is what makes an AsyncStore and an RPCStore
    interchangeable, and an exception type is part of a result - so
    `except BadStorePath` has to work against the protocol and not
    only against the compiled binding.

    `_runtime._invoke` wraps anything without a `to_dict` in
    InternalError and keeps the real error as `__cause__`. A declared
    Nix error has no `to_dict`, so it arrives as somebody else's
    cause.

    Written as an assertion of the CURRENT behaviour rather than as
    an xfail, and deliberately: the wrapped form is checked too, so
    the day a declared error passes through untouched this fails and
    says to delete the branch. An xfail would have gone quiet
    instead - and it cannot be applied per-surface anyway, because
    one of the three already behaves.

    tasks/066 has the finding and why the fix is not a one-liner."""
    from huggorm_generated._runtime import InternalError

    if surface_of(obj) == "sync":
        with pytest.raises(kind):
            yield
        return
    with pytest.raises(InternalError) as caught:
        yield
    cause = caught.value.__cause__
    assert isinstance(cause, kind), (
        f"{surface_of(obj)}: the declared error should reach the caller "
        f"as {kind.__name__}; it is wrapped, and the cause is {cause!r}")


async def test_a_bad_path_raises_the_same_class_everywhere(
        store: Any) -> None:
    from huggorm_bindings.errors import BadStorePath

    with declared_error(BadStorePath, store):
        await call(store, "parse_store_path", "/somewhere/else/x")


async def test_an_unsupported_operation_raises_the_same_class(
        store: Any) -> None:
    """A statement about the store, not a failure of the call.

    nix::Store gives a default implementation for methods only some
    stores can answer, and that default throws. dummy:// cannot list
    its paths, and the caller must learn that the same way on every
    surface - it is the difference between falling back to another
    store and giving up."""
    from huggorm_bindings.errors import Unsupported

    with declared_error(Unsupported, store):
        await call(store, "query_all_valid_paths")


# -- proxies ----------------------------------------------------------
# A Value stays where it is. Locally that is the object itself; over
# the wire it is a handle, and the surface a caller sees is an
# RPCValue wrapping it. The point of these is that the caller cannot
# tell.


async def test_an_evaluated_integer_is_the_same_everywhere(
        state: Any) -> None:
    v = await call(state, "eval_expr", "1 + 2")
    assert await call(v, "type_name") == "int"
    assert await call(v, "integer") == 3


async def test_a_built_value_reads_back_the_same_everywhere(
        state: Any) -> None:
    """Built rather than evaluated, which is the other direction: a
    scalar goes IN as itself and comes back as a proxy."""
    v = await call(state, "make_string", "hello")
    assert await call(v, "type_name") == "string"
    assert await call(v, "string_value") == "hello"


async def test_a_list_walks_the_same_everywhere(state: Any) -> None:
    """A container of proxies. Each element is itself a Value, so this
    is where a surface that adopted an element onto the wrong runner -
    or forgot to adopt it at all - stops agreeing."""
    v = await call(state, "eval_expr", "[ 1 2 3 ]")
    assert await call(v, "type_name") == "list"
    assert await call(v, "size") == 3
    items = [await call(await call(v, "at", i), "integer") for i in range(3)]
    assert items == [1, 2, 3]


async def test_an_attribute_set_reads_the_same_everywhere(
        state: Any) -> None:
    v = await call(state, "eval_expr", '{ a = 1; b = "two"; }')
    assert await call(v, "type_name") == "attrs"
    assert await call(v, "size") == 2
    assert await call(v, "has", "a") is True
    assert await call(await call(v, "get", "a"), "integer") == 1
    assert await call(await call(v, "get", "b"), "string_value") == "two"


async def test_a_bad_expression_raises_the_same_class_everywhere(
        state: Any) -> None:
    """An error from the EVALUATOR, which is a different translator
    from the store's and reaches a caller by the same route."""
    from huggorm_bindings.errors import NixError

    with declared_error(NixError, state):
        await call(state, "eval_expr", "not an expression")
