# A StorePath should be easy to open, read and walk

Carl, 2026-08-26: "Something that'd be really cool is if StorePath
could inherit from pathlib.Path / anyio.Path (or have a method to
construct them easily) so there's an easy API for interacting with
them from Python, maybe deserves an issue and some consideration for
what's best."

The want is right. `add_path_to_store` hands back a StorePath, and the
only thing a caller can currently do with it is ask the store to print
it and then build a `pathlib.Path` from the string by hand - which the
test for that method does, wrongly the first time.

## What a StorePath actually is

Not a filesystem path. `nix::StorePath` is `<hash>-<name>` and nothing
else: no directory, no store, no machine. That is why
`print_store_path` and `parse_store_path` live on the STORE - only the
store knows its own directory.

Two things follow, and they decide most of this:

- **The store directory is not the real directory.** A chroot store
  keeps `/nix/store` as its store directory and puts the files under
  `<root>/nix/store`. So `print_store_path` answers with a location
  that does not exist on this machine. libstore has a separate call
  for the real one, `toRealPath`.
- **`toRealPath` is on `LocalFSStore`, not on `Store`.** A binary
  cache, an S3 store, an ssh-ng store: none of them have a real path,
  because the bytes are not on this filesystem. Upstream put the
  method exactly where the answer exists.

So "a StorePath as a pathlib.Path" is not a property of the StorePath.
It is a question that needs a store, and one that a store may honestly
refuse to answer.

## Can it inherit? No.

Probed, not assumed.

`cdef class StorePath(pathlib.Path)` does not compile:

    pathprobe.pyx:4:22: First base of 'Sub' is not an extension type

A cdef class may only inherit from another extension type. StorePath
is a cdef class because it owns a `nix::StorePath *`, so this is a
hard stop rather than a preference.

`anyio.Path` would not help either: its MRO is `(anyio.Path, object)`.
It is a wrapper around an `os.PathLike`, not a `pathlib.Path`
subclass, so inheriting from it buys none of pathlib's surface.

A pure-Python subclass of `pathlib.Path` DOES work cleanly on 3.12+ -
`with_segments` propagates the subclass, so `p / "bin"` stays the
subclass. That is worth knowing, but it is the wrong shape here for
the reason above: such a class would have to carry the absolute path,
which means it is already store-resolved and is no longer a StorePath.

## The options, and what each one costs

**A. `__fspath__` on StorePath.** One dunder makes it `os.PathLike`,
so `open(sp)`, `pathlib.Path(sp)` and `shutil` all work with no new
API. Tempting and wrong: `__fspath__` must return an absolute path,
and a StorePath does not know one. It would have to assume
`/nix/store`, which is a lie for every chroot store - including the
one the whole hermetic test suite runs against. A silent wrong path is
worse than no method.

**B. A method on the store.** `store.real_path(path) -> pathlib.Path`,
backed by `LocalFSStore::toRealPath`. Correct by construction: it is
on the object that knows, it fails for a store with no filesystem, and
it answers where the bytes REALLY are rather than where they nominally
live.

This is also the first binding that wants the store hierarchy for a
real reason. `Store` is abstract and opened by URI; only some
implementations are `LocalFSStore`. Today the binding has one `Store`
class, so either it declares the method and raises for a store that
cannot answer - which is what `query_all_valid_paths` already does,
with libstore's own "not supported by store" - or the hierarchy grows
a `LocalFSStore` binding. The first is cheaper and matches an existing
precedent; the second is more honest and is what 018 built the
generated-base machinery for.

**C. A path object that carries its store.** `store.open(path)` giving
something that knows both. More API, and it invites the thing option A
gets wrong: a handle that looks like a file and is not one when the
store is remote.

**Recommendation: B**, and start with the method on `Store` raising
libstore's error, because that needs no new class and no new
declaration. Grow the hierarchy when a second `LocalFSStore`-only
method wants it.

## The async and RPC half

This is where it gets interesting, and where it should NOT be rushed.

Over RPC the real path is on the SERVER's filesystem. Handing an
`anyio.Path` back to a client that will happily `await p.read_text()`
is the same footgun as option A, one layer out: the object works, and
it reads a different machine's file or nothing at all.

Three sub-questions, none of them answered yet:

1. What does the codegen do with `pathlib.Path` as a return type? It
   is not a wire scalar. Either the codegen learns it the way it
   learned `bytes` and `StrEnum` - a scalar whose constructor is
   `pathlib.Path` - or the declared return stays `str` and the
   convenience lives above the generated surface.
2. Should the RPC surface offer it at all? A method that means
   "a path on whichever machine answered" is honest only if the caller
   can tell which machine that is. Blocking it on the wire, the way a
   proxy parameter is blocked, is a legitimate answer.
3. `anyio.Path` is a wrapper, so the async surface can return
   `anyio.Path(sync_result)` with no new type. That is a one-line
   adaptation IF the sync answer is right, which is the whole reason
   to settle the sync side first.

## What to do first

`Store.real_path(path) -> pathlib.Path`, in process, returning the
REAL directory and raising libstore's error for a store that has none.
Test it against the chroot store, which is the case that proves the
distinction: the printed path and the real path differ there, and the
test helper in test_store.py currently reconstructs the real one by
hand.

Then decide the wire question with a working local method in hand,
rather than in the abstract.
