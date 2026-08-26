# Quickstart

Nix, from Python. One import.

    import cythonix

    store = cythonix.Store("auto")

`"auto"` is whatever the ambient configuration says, usually the
daemon. `"dummy://"` is in-memory and needs nothing on disk.
`"/tmp/mystore"` is a chroot store, which needs no daemon and no
`/nix/var` - it is what this repo's own tests run against.

## A store path is a name, not a location

    path = store.add_to_store("hello", b"hello\n")
    path                       # StorePath(base_name='1q8...-hello')
    store.print_store_path(path)   # /nix/store/1q8...-hello
    store.real_path(path)          # where the bytes REALLY are

The three are different questions. A `StorePath` is `<hash>-<name>`
and nothing else - no directory, no store, no machine - which is why
printing one needs the store. `real_path` differs again: a chroot
store keeps `/nix/store` as its store directory while the files live
under `<root>/nix/store`.

Going the other way, from a file to the object that holds it:

    where = store.to_store_path("/nix/store/1q8...-python/bin/python3")
    where.path()        # StorePath('1q8...-python')
    where.sub_path()    # '/bin/python3'

    store.follow_links_to_store_path("./result")   # resolves the link first

## What the store knows

    info = store.query_path_info(path)
    info.nar_size()            # bytes, of the NAR
    info.deriver()             # the .drv that built it, or None
    info.ca()                  # 'fixed:r:sha256:...', or None if built
    info.references()          # [StorePath, ...] - what it points at

`None` is a real answer in both places: a path that was ADDED has no
deriver, and one that was BUILT has no content address.

## The graph

One edge is a fact; the closure is what you can copy, sign or delete
as a unit.

    store.query_referrers(path)          # what points AT it
    store.compute_fs_closure([path])     # everything reachable, transitively
    store.compute_fs_closure([path], flip_direction=True)
                                         # what would break if it went away

Store paths compare and hash, so these read the way you would write
them:

    closure = set(store.compute_fs_closure([a, b]))
    assert a in closure

## Async

The same calls, awaited, with the blocking part moved onto a thread so
your event loop keeps running.

    import anyio
    import cythonix

    async def main():
        store = cythonix.AsyncStore("auto")
        path = await store.add_to_store("hello", b"hello\n")
        print(await store.print_store_path(path))
        await store.aclose()

    anyio.run(main)

`aclose()` shuts the runner's thread down. Every async object has one.

Four blocking calls run at once by default. If your application wants
more, say so before the first call:

    cythonix.set_pool_size(16)

## Remote

The same protocol, on someone else's store.

    server:  await cythonix.serve(host="127.0.0.1", port=50051)

    client:  client = await cythonix.connect("127.0.0.1", 50051)
             store = await client.acquire("Store", "auto")
             path = await store.add_to_store("hello", b"hello\n")

`store` here is an `RPCStore`, and the in-process one is an
`AsyncStore`. Both satisfy `cythonix.StoreLike`, so code written
against the protocol runs either way:

    async def total_size(store: cythonix.StoreLike, path) -> int:
        info = await store.query_path_info(path)
        return info.nar_size()

Values come back as real local objects - a `PathInfo` is a copy of
what the store said, not a handle - so reading its fields costs no
round trip. A `Store` is a proxy: it is a connection, and identity
matters.

If the server sweeps your connection for being silent, the next call
raises `cythonix.ConnectionExpired` rather than failing later as an
unknown handle. Recovery is a fresh `connect()` and re-acquiring what
you held.

## Errors

    try:
        store.query_path_info(cythonix.StorePath("0" * 32 + "-nope"))
    except cythonix.errors.InvalidPath:
        ...

They are libstore's own, with libstore's own messages, and they keep
their class across the wire. `InvalidPath` means the store does not
have that path; `BadStorePath` means the string is not a store path at
all. The colour libstore writes into a message is kept as a separate
field rather than printed at you.

## Where to look next

`README.md` is contributor-facing: how the four layers are built, what
the build refuses, and how to add a binding. `tasks/` holds one file
per design decision.
