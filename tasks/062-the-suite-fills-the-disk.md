# The suite fills the disk

**OPEN.** The test suite leaves 463 MB in `/tmp` per run, and a
failed cleanup path lets that accumulate without bound. It reached
16 GB and filled a 61 GB root, which stopped a build with a SIGBUS
inside sqlite.

## What was measured

On 2026-08-28, `/tmp/pytest-of-lillecarl` held **16 GB** across 139
directories, every one of them named `garbage-*`. The disk was at
100%, 80 MB free.

The failure it caused is worth naming, because it does not look like
a disk problem:

    nix::LocalStore::openDB -> sqlite3_exec -> Bus error (core dumped)

sqlite mmaps its database. A write to a mapped page with no space
behind it is SIGBUS, not ENOSPC, so a full disk arrives as a segfault
in a library that has nothing to do with the cause.

After deleting it, one clean run of `nix run --file . test` left
**463 MB**.

## Where the 463 MB is

    55 x 8.0M   nix/var/nix/db/reserved

`nix::LocalStore` pre-allocates a reserved file when it opens a store,
so it can free space during a garbage collection on a full disk. It is
8 MiB, and it is per STORE.

The `chroot` fixture in `test_store.py` is function-scoped over
`tmp_path`, so every test that asks for a real store gets its own
store, its own database and its own 8 MiB reservation. 55 of them in
one run.

`libstore` has a setting for this - `reserved-size` - and 0 is a legal
value for a store that is never garbage-collected. Whether the fixture
should set it, or share one store across a module, or something else,
is the decision this task is for. Sharing has a known hazard already
recorded in `declare.py`: two LocalStores in one process deadlock on a
temp-roots flock, so "just use a session fixture" is not free.

## Where the 16 GB is

A different fault, and the one that actually matters, because 463 MB
per run is bounded and 16 GB is not.

pytest keeps the last three runs and removes the rest. When a removal
FAILS it renames the directory to `garbage-*` and tries again next
time. Every one of the 139 was a `garbage-*`, so removal had been
failing for a long time and each attempt only added another.

The warnings say why:

    (rm_rf) error removing .../test_a_build_lands_in_the_uppe0/lower/nix/store/...-ncurses-6.6
      OSError: [Errno 30] Read-only file system
      OSError: [Errno 39] Directory not empty
      PermissionError: [Errno 13] Permission denied

Store paths are read-only by design, and a directory of them cannot be
removed without making it writable first. `shutil.rmtree` does not do
that, and neither does pytest.

**`test_a_build_lands_in_the_upper*` does not exist in this repo any
more.** The name survives only in the leftover directories, so some of
those 139 are from a test that has since been renamed or deleted. That
does not make the fault historical: any test that copies store paths
into `tmp_path` reproduces it.

## What to decide

1. Should the fixture drop `reserved-size`, share a store, or neither?
   The flock hazard rules out the obvious version of sharing.
2. Should a fixture make its tree writable before pytest tries to
   remove it? A `chmod -R u+w` finalizer is one line and it is the
   difference between bounded and unbounded growth.
3. Is 463 MB per run acceptable once the growth is bounded? Three
   retained runs is 1.4 GB, which is fine on a big disk and not on a
   laptop.

## What was NOT done

Nothing is fixed. The 16 GB was deleted so work could continue, and
that is all. The numbers above are from a clean run afterwards, so
they are current rather than remembered.
