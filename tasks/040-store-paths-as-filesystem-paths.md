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

## The wire question, answered

Carl, mid-implementation: "I don't think this has to go over the wire
at all, it can just be a method?"

Right, and it is the better answer. `pathlib.Path` is not a wire type,
and teaching the schema to carry one would have bought a remote method
whose result names a filesystem the caller cannot reach.

What it needed instead was a gap in the codegen. A FREE FUNCTION could
already say "the wire cannot carry this" - `wire_blocker` reports it,
the manifest records it, and the build prints it. A METHOD could not:
every method of a wrapped class got an rpc unconditionally, and a type
the schema could not represent raised while BUILDING the schema. So
methods now get the same treatment, and one piece of knowledge -
`wire_blocker` - decides both.

A blocked method leaves three places and keeps one:

- no message in the schema, so nothing describes a call that cannot
  happen;
- no handler on the server;
- off the protocol, because a protocol is what BOTH implementations
  satisfy - a wire blocker IS a protocol blocker, and surface.py reads
  grpc_schema's answer rather than deciding again;
- and it keeps its in-process wrapper, which is the whole point.

The emitter also learned that an annotation may name a MODULE. A
declared type is normally a builtin or a binding class; `pathlib.Path`
is neither, and an emitted module that annotates with it needs a plain
`import pathlib`. That list lives in wiretypes.py, beside the scalars,
because it is a fact about how a type is SPELLED. It is explicitly not
a wire type - nothing on it crosses.

## The async half

`anyio.Path` is a wrapper around an `os.PathLike`, so the async
wrapper could return `anyio.Path(sync_result)` with no new type. It
does not yet, and there is no hurry: the emitted wrapper hops to a
thread, and what it hands back is a `pathlib.Path` that a caller can
wrap themselves.

Worth doing when something wants async file IO on a store path. Worth
NOT doing as a reflex: an `anyio.Path` is only useful if the caller
awaits reads on it, and that is a different feature from naming a
location.

## Done, in part

`Store.real_path(path) -> pathlib.Path` exists. It answers with the
REAL directory - `<root>/nix/store/...` for a chroot store, where
`print_store_path` answers `/nix/store/...` and that does not exist -
and it raises `Unsupported` for a store with no filesystem, by asking
whether the store is a `LocalFSStore` the way libstore's own code
does.

It is in process only, deliberately, and the codegen now has a way to
say that.

## Still open

`add_path_to_store` takes a path the STORE reads, and that one DOES
cross the wire - as a string, which is what it is. The asymmetry is
real and defensible: sending a path to a store is a normal thing to
do, and receiving one back invites opening it. It is worth a second
look if a caller ever gets confused by it.

Nothing here gives a StorePath a `pathlib` surface of its own. That
was the original ask and it stays refused for the reason at the top: a
StorePath is a name, not a location, and only a store can turn one
into the other.
