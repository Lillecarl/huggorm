"""One configuration scope onto local Nix, and the objects it made.

A session owns a default store URI and every store and evaluator
handed out from it. Two sessions with different URIs coexist in one
process. An evaluator only accepts a store from its own session.

This is the local async flavour. A sync `Session` and a remote
session follow, sharing the protocol below.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from huggorm_generated import AsyncEvalState, AsyncStore

if TYPE_CHECKING:
    from huggorm_bindings import Store


@runtime_checkable
class AsyncSessionLike(Protocol):
    """What every local async session promises."""

    __slots__ = ()

    def store(self, uri: str | None = None) -> AsyncStore:
        """A store on this session, not yet opened."""
        ...

    def eval(
        self,
        store: AsyncStore,
        settings: dict[str, str] | None = None,
        build_store: Store | None = None,
    ) -> AsyncEvalState:
        """An evaluator bound to `store`, not yet opened."""
        ...

    async def aclose(self) -> None:
        """Release every object this session handed out. Idempotent."""
        ...


class AsyncSession:
    """A scope for local async Nix objects.

    `store()` and `eval()` build wrappers lazily: nothing runs until
    the first call. `aclose()` shuts the evaluators down, each on its
    own thread, then the stores. Stores share the process pool, so
    closing one releases nothing but the wrapper.
    """

    def __init__(self, store_uri: str = "auto") -> None:
        self._store_uri = store_uri
        self._stores: set[AsyncStore] = set()
        self._evals: set[AsyncEvalState] = set()
        self._closed = False

    def store(self, uri: str | None = None) -> AsyncStore:
        """Make a store, tracked by this session.

        `None` means the session default. The wrapper builds lazily,
        so this never touches Nix.
        """
        store = AsyncStore(uri if uri is not None else self._store_uri)
        self._stores.add(store)
        return store

    def eval(
        self,
        store: AsyncStore,
        settings: dict[str, str] | None = None,
        build_store: Store | None = None,
    ) -> AsyncEvalState:
        """Make an evaluator on a store this session made.

        A foreign store raises `ValueError`. An evaluator reads its
        store's configuration at build time, so one from another
        session would silently keep the wrong one.
        """
        if store not in self._stores:
            raise ValueError(
                "this store belongs to another session; "
                "make the evaluator where the store was made"
            )
        state = AsyncEvalState(store, settings, build_store)
        self._evals.add(state)
        return state

    async def aclose(self) -> None:
        """Close the evaluators, then the stores, and report together.

        Evaluators go first: a store cannot shut down while an
        evaluator still reads it. Each failure is collected, so the
        first one does not strand the rest. One failure raises alone;
        several raise as an `ExceptionGroup`. A closed session stays
        closed.
        """
        if self._closed:
            return
        self._closed = True
        errors: list[Exception] = []
        for state in tuple(self._evals):
            try:
                await state.aclose()
            except Exception as exc:
                errors.append(exc)
        self._evals.clear()
        for store in tuple(self._stores):
            try:
                await store.aclose()
            except Exception as exc:
                errors.append(exc)
        self._stores.clear()
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise ExceptionGroup("closing the session failed", errors)

    async def __aenter__(self) -> AsyncSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()
