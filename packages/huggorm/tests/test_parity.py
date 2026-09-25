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


async def open_state(surface: str, client: Any,
                     settings: dict[str, str] | None = None) -> Any:
    """One evaluator of the named surface.

    A function rather than only a fixture, because a test that is
    ABOUT the state needs a second one - and a second fixture over
    the same three surfaces would ask for nine combinations to answer
    a question about three."""
    if surface == "sync":
        from huggorm_bindings import EvalState

        return EvalState(URI, settings)
    if surface == "async":
        from huggorm_generated import AsyncEvalState

        return AsyncEvalState(URI, settings)
    return await client.acquire("EvalState", URI, settings)


@pytest.fixture(params=SURFACES)
def surface(request: Any) -> str:
    """WHICH of the three a test is running against.

    Carries the parameterisation so `state` does not, which lets a
    test ask for both: `state` for the one under test, and `surface`
    for opening a second of the same kind."""
    return str(request.param)


@pytest.fixture
async def state(surface: str, client: Any) -> Any:
    """An evaluator, opened three ways.

    The companion to `store` and a different shape of problem: a Store
    is POOL-threaded and an EvalState is AFFINE, so the async and rpc
    surfaces pin it to one thread. A caller cannot tell, and this
    file is where that claim is checked rather than asserted."""
    return await open_state(surface, client)


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
# The hardest thing to keep identical, and the one place the three
# surfaces used to disagree. Locally a C++ exception is translated
# into a Python one by the binding; remotely it is encoded into a
# status detail, sent, and rebuilt from its declared parts.
#
# `except BadStorePath` once worked against the compiled binding and
# against neither generated surface: both wrapped it in an
# InternalError and kept it as __cause__. This suite found that on its
# first run and pinned it with a helper that asserted BOTH shapes; the
# helper is gone because there is now one shape (tasks/066).
#
# So these are written the way a CALLER writes them. That is the
# claim: an exception type is part of a result, and `StoreLike` makes
# the three interchangeable.


async def test_a_bad_path_raises_the_same_class_everywhere(
        store: Any) -> None:
    from huggorm_bindings.errors import BadStorePath

    with pytest.raises(BadStorePath):
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

    with pytest.raises(Unsupported):
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


async def test_an_evaluated_float_is_the_same_everywhere(
        state: Any) -> None:
    """A double on the wire, so a value no int can carry must survive."""
    v = await call(state, "eval_expr", "0.1 + 0.2")
    assert await call(v, "type_name") == "float"
    assert await call(v, "floating") == 0.1 + 0.2
    built = await call(state, "make_float", -2.5)
    assert await call(built, "floating") == -2.5


async def test_an_int_is_not_a_float(state: Any) -> None:
    v = await call(state, "eval_expr", "1")
    with pytest.raises(Exception, match="float"):
        await call(v, "floating")


async def test_a_state_takes_its_own_settings_everywhere(
        surface: str, client: Any) -> None:
    """The service case: a remote client asks for a pure evaluator.
    The settings cross as a map, in `Acquire` on the rpc surface."""
    probe = "builtins ? currentTime"
    pure = await open_state(surface, client, {"pure-eval": "true"})
    assert await call(await call(pure, "eval_expr", probe), "boolean") is False
    plain = await open_state(surface, client)
    assert await call(await call(plain, "eval_expr", probe), "boolean") is True


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

    with pytest.raises(NixError):
        await call(state, "eval_expr", "not an expression")


async def test_a_file_evaluated_twice_is_read_once(
        surface: str, state: Any, client: Any, tmp_path: Any) -> None:
    """The warm cache, on every surface.

    `evalFile` keeps a `fileEvalCache` keyed by resolved path, and a
    hit forces the value it already has and copies it - it never opens
    the file (`eval.cc:1118`). So DELETING the file is the observable:
    the second call can only be answered from the cache.

    An int, not an attribute set, and not an expression that imports
    another file. `evalFile` forces to WHNF, so a value that is not
    complete there would leave a thunk that might still want the
    file - which would make a failure mean the wrong thing.

    The third part is why this matters to `tasks/016`: the cache
    belongs to the STATE. A fresh evaluator has to read, so a state
    that dies takes the warm work with it - which is what makes "the
    same state, claimed later" the milestone rather than a
    convenience."""
    src = tmp_path / "answer.nix"
    src.write_text("40 + 2\n")

    first = await call(state, "eval_file", str(src))
    assert await call(first, "integer") == 42

    src.unlink()
    again = await call(state, "eval_file", str(src))
    assert await call(again, "integer") == 42

    cold = await open_state(surface, client)
    with pytest.raises(Exception) as caught:
        await call(cold, "eval_file", str(src))
    # "opening file", not merely the name: it is the cold state SAYING
    # it went to disk, which is the half the warm call is claimed not
    # to do. Measured identical on all three surfaces -
    #   SysError: error: opening file '.../answer.nix':
    #   No such file or directory
    # - so this is also where the RPC translation of a libutil error
    # would stop agreeing.
    assert "opening file" in str(caught.value)
    assert "answer.nix" in str(caught.value)


async def test_a_file_reached_by_import_is_in_the_cache_too(
        state: Any, tmp_path: Any) -> None:
    """What a watcher would watch, and why it is not our own boundary.

    `tasks/016` wants a change to a file an evaluation read to
    invalidate the warm state. Our binding sees ONE path - the one
    handed to `eval_file` - and an evaluation reads many: `import`
    goes through `evalFile` too, so the file it names is cached
    exactly like the file the caller named.

    That difference is the whole reason `cached_files` reaches into
    libexpr's own cache rather than counting what we were asked to
    evaluate. The inner file is the assertion that matters; the outer
    one is the control that says the answer is not empty for some
    other reason.

    Both are asserted absent BEFORE the evaluation, so a state that
    answered with every file it had ever seen - or with a constant -
    would fail here rather than pass by accident."""
    inner = tmp_path / "inner.nix"
    inner.write_text("40 + 2\n")
    outer = tmp_path / "outer.nix"
    outer.write_text(f"import {inner}\n")

    before = await call(state, "cached_files")
    assert str(inner) not in before and str(outer) not in before

    v = await call(state, "eval_file", str(outer))
    assert await call(v, "integer") == 42

    files = await call(state, "cached_files")
    assert str(outer) in files, files
    assert str(inner) in files, "an imported file is cached too"


async def _closure(state: Any, path: str) -> list[str]:
    """The files one evaluation read, as the cache diff around it.

    `cached_files` before and after a single `eval_file` differ by
    exactly what that evaluation cached, and libexpr offers no other
    way to ask - it keeps no edge from an importer to its import.

    A helper in the TEST rather than in the binding, because it is a
    policy and not a fact: a caller who evaluates two files
    concurrently on one state gets their closures mixed. The affine
    state this repo already assumes is what makes it work here."""
    before = set(await call(state, "cached_files"))
    await call(state, "eval_file", path)
    return [f for f in await call(state, "cached_files") if f not in before]


async def test_forgetting_a_closure_picks_up_an_edited_import(
        state: Any, tmp_path: Any) -> None:
    """Per-path invalidation, which is what a reload server needs.

    Carl's requirement, in his words: a live-reloading evaluation
    server, and "resetting the entire eval cache database is not what
    we want to do at all". `resetFileCache()` is the only public way,
    and it also drops the fetched flake inputs - re-downloading every
    input because one local file changed.

    The CLOSURE, not the file, and `tasks/016` records the
    measurement that says so: the cache holds no edge from an importer
    to its import, so forgetting `inner` alone leaves `outer`
    answering its old value. The negative control below is that
    measurement, kept as a test.

    Two evaluations of the same path in one state, with an edit
    between them, and the second answers the NEW value. That is the
    whole claim."""
    inner = tmp_path / "inner.nix"
    inner.write_text("40 + 2\n")
    outer = tmp_path / "outer.nix"
    outer.write_text(f"import {inner}\n")

    closure = await _closure(state, str(outer))
    assert str(outer) in closure and str(inner) in closure, closure

    inner.write_text("1 + 1\n")
    for path in closure:
        await call(state, "forget_file", path)

    again = await call(state, "eval_file", str(outer))
    assert await call(again, "integer") == 2, "the edit was not picked up"


async def test_forgetting_only_the_edited_file_leaves_the_importer_stale(
        state: Any, tmp_path: Any) -> None:
    """The negative control for the test above, and a real limit.

    Identical, except that it forgets the file that CHANGED instead of
    the closure that read it. The importer keeps answering 42 from a
    cache entry whose input now says 2, and nothing complains - a
    silent stale answer, which is the failure this design is shaped to
    avoid.

    Asserted rather than merely noted, so `forget_file` growing a
    recursive erase would fail here and be seen. It is not a wish that
    the answer stays stale; it is the statement that per-file erase
    ALONE is not invalidation."""
    inner = tmp_path / "inner.nix"
    inner.write_text("40 + 2\n")
    outer = tmp_path / "outer.nix"
    outer.write_text(f"import {inner}\n")

    first = await call(state, "eval_file", str(outer))
    assert await call(first, "integer") == 42

    inner.write_text("1 + 1\n")
    await call(state, "forget_file", str(inner))

    again = await call(state, "eval_file", str(outer))
    assert await call(again, "integer") == 42, "no edge means no cascade"


async def test_forgetting_a_directory_forgets_its_default_nix(
        state: Any, tmp_path: Any) -> None:
    """The other spelling, which the closure gate cannot reach.

    `fileEvalCache` is keyed by the RESOLVED path and
    `importResolutionCache` holds the resolution, so the two caches
    disagree about what a directory is called. Evaluating `/dir`
    caches `/dir/default.nix`, and forgetting the name the CALLER used
    would erase a key that was never there.

    The closure gate above never asks this: `outer.nix` and
    `inner.nix` resolve to themselves, so erasing the given path is
    enough there and the resolution lookup could be deleted without a
    test noticing.

    So this forgets the DIRECTORY only, and never names the file. A
    stale 42 is what an erase of the given spelling alone would
    leave."""
    src = tmp_path / "dir"
    src.mkdir()
    (src / "default.nix").write_text("40 + 2\n")

    first = await call(state, "eval_file", str(src))
    assert await call(first, "integer") == 42

    (src / "default.nix").write_text("1 + 1\n")
    await call(state, "forget_file", str(src))

    again = await call(state, "eval_file", str(src))
    assert await call(again, "integer") == 2, "the resolved key survived"
