# This file demonstrates the key learning goal:
# Python subclasses of Cython-bound C++ classes.
#
# Two patterns, now on the Store hierarchy:
# 1. Subclassing concrete LocalStore — Python-only override (no C++
#    trampoline). `class LoudLocal(LocalStore): def get_uri():` works in
#    Python but C++ describe() still sees "local". Good for pure-Python
#    extensions.
# 2. Subclassing abstract Store — trampoline (PyStore) makes Python
#    overrides visible to C++. `class MyCache(Store):` + describe(cache)
#    goes through C++ virtual dispatch and sees the Python get_uri.

from fake_library import (
    Store,
    LocalStore,
    RemoteStore,
    StorePath,
    DerivedPath,
    describe,
)


class LoudLocal(LocalStore):
    """A Python subclass that overrides get_uri()."""

    def get_uri(self) -> str:
        return "local-loud"


class MyCache(Store):
    """Trampoline demo: Python subclass of abstract Store is visible to C++."""

    def get_uri(self) -> str:
        return "https://my-cache.example.com"


def demo():
    local = LocalStore()
    remote = RemoteStore()
    loud = LoudLocal()
    cache = MyCache()

    print(f"local:   uri={local.get_uri()}")
    print(f"remote:  uri={remote.get_uri()}")
    print(f"loud:    uri={loud.get_uri()}")

    print("\n--- Trampoline: Store subclass visible to C++ ---")
    # C++-level via describe() - goes through C++ virtual dispatch + PyStore
    print(f"MyCache C++ describe: {describe(cache)}")
    print(f"local C++ describe:   {describe(local)}")

    print("\n--- Limitation demo: LocalStore subclass NOT visible to C++ ---")
    print(f"LoudLocal Python get_uri: {loud.get_uri()}")
    print(f"LoudLocal C++ describe:   {describe(loud)}  # still 'local', not 'local-loud'")
    print("-> leaves have no trampoline; override is Python-only. Store has one.")

    print("\n--- Value types come from stores, not constructors ---")
    p = local.add_text_to_store("greeting.txt", "hi")
    print(f"path: {p.to_string()} hash={p.hash_part()} name={p.name_part()}")
    drv_path = local.add_text_to_store("demo.drv", "DrvDemo")
    drv = local.query_derivation(drv_path)
    print(drv.describe())
    req = DerivedPath(drv_path, "out")
    print(f"request: {req.describe()}")
    out = local.build_derivation(req)
    print(f"built output: {out.to_string()} valid={local.is_valid_path(out)}")

    print("\n--- Wire-value copies (immutable types travel by copy) ---")
    import copy
    p2 = copy.copy(p)
    print(f"store path copy: distinct={p2 is not p}, equal={p2.to_string() == p.to_string()}")
    req2 = copy.copy(req)
    print(f"request copy:    distinct={req2 is not req}, equal={req2.describe() == req.describe()}")

    try:
        Store()
    except TypeError as e:
        print(f"\nStore() correctly raises: {e}")
    try:
        StorePath()
    except TypeError as e:
        print(f"StorePath() correctly raises: {e}")


if __name__ == "__main__":
    demo()
