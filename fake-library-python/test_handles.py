"""
HandleTable unit tests (tasks/002, tasks/031).

The lifecycle suite drives the table through a real gRPC server, which
is what proves the transport. It cannot reach the cases that only the
table can produce: the same object handed out twice, a producer handle
that does not exist, an object index that must not outlive its entry.

Every step calls audit(). That method held the one invariant the whole
model rests on and nothing called it, so a lease created or destroyed
by accident stayed invisible until a handle leaked much later.

Run:  nix run --file . ourPython -- test_handles.py
"""

from typing import Any

from fake_library_python.lifecycle import ANON, HandleTable
from test_remote import check


class Obj:
    """Stand-in for a wrapper object. Identity is all the table uses."""

    def __init__(self, name: str = "") -> None:
        self.name = name


def table(ttl: float | None = 120.0) -> HandleTable:
    return HandleTable(ttl=ttl)


def test_identity_mapping() -> None:
    t = table()
    a = t.bind()
    obj = Obj("value")

    h1 = t.put(obj, a)
    h2 = t.put(obj, a)
    t.audit()
    check("same object, same connection: one handle", h1 == h2, h1[:8])
    check("second put adds a lease", t.entries[h1].leases == 2)
    check("one entry, not two", len(t.entries) == 1, len(t.entries))

    # Releasing once must NOT drop it: the second put took a lease.
    t.release(a, h1)
    t.audit()
    check("one release leaves the object alive", h1 in t.entries)
    t.release(a, h1)
    t.audit()
    check("the matching release drops it", h1 not in t.entries)


def test_distinct_objects_get_distinct_handles() -> None:
    t = table()
    a = t.bind()
    h1 = t.put(Obj("one"), a)
    h2 = t.put(Obj("two"), a)
    t.audit()
    check("distinct objects: distinct handles", h1 != h2)


def test_identity_is_per_connection() -> None:
    t = table()
    a, b = t.bind(), t.bind()
    obj = Obj("shared")
    ha = t.put(obj, a)
    hb = t.put(obj, b)
    t.audit()
    check("two connections get two handles for one object", ha != hb)
    check("both handles resolve to the same object",
          t.get(ha) is t.get(hb) is obj)
    t.release(a, ha)
    t.audit()
    check("one connection releasing does not drop the other's handle",
          ha not in t.entries and hb in t.entries)


def test_index_does_not_outlive_the_entry() -> None:
    """A dropped handle must leave no index row behind. A stale row
    would hand a caller a handle id that no longer exists, and the
    grant would raise on an entry that is gone."""
    t = table()
    a = t.bind()
    obj = Obj("recycled")
    h1 = t.put(obj, a)
    t.release(a, h1)
    t.audit()
    check("dropped entry clears the object index", not t._by_obj.get(a))
    h2 = t.put(obj, a)
    t.audit()
    check("the same object after a drop gets a fresh handle", h2 != h1)
    check("and it works", t.get(h2) is obj)


def test_dead_connection_clears_its_index() -> None:
    t = table(ttl=1.0)
    a, b = t.bind(), t.bind()
    obj = Obj("pinned")
    ha = t.put(obj, a)
    hb = t.put(obj, b)
    # b stays alive; a goes silent past the TTL.
    t.connections[a].last_seen -= 10.0
    dropped = t.sweep()
    t.audit()
    check("the silent connection's handle drops", ha in dropped, dropped)
    check("the live connection keeps its own", hb in t.entries)
    check("the dead connection's index bucket is gone", a not in t._by_obj)
    check("the survivor's entry forgot the dead token",
          a not in t.entries[hb].tokens)


def test_share_indexes_the_target() -> None:
    """After a share the target holds the object under that handle, so
    a later put() for the target must reuse it."""
    t = table()
    a, b = t.bind(), t.bind()
    obj = Obj("handed over")
    ha = t.put(obj, a)
    t.share(a, b, ha, "copy")
    t.audit()
    hb = t.put(obj, b)
    t.audit()
    check("put after share reuses the shared handle", hb == ha)
    check("and it took a lease rather than a second handle",
          len(t.entries) == 1 and t.entries[ha].leases == 3,
          t.entries[ha].leases)


def test_transfer_leaves_the_source_indexed() -> None:
    """transfer moves the lease but the source may still be indexed.
    A put() there must not resurrect a lease it does not hold - it
    takes a fresh one, which audit has to balance."""
    t = table()
    a, b = t.bind(), t.bind()
    obj = Obj("moved")
    ha = t.put(obj, a)
    t.share(a, b, ha, "transfer")
    t.audit()
    check("transfer left the source holding nothing", ha not in t.connections[a].leases)
    again = t.put(obj, a)
    t.audit()
    check("the source can take a new lease on the same handle", again == ha)
    check("and the entry counts both holders", t.entries[ha].leases == 2)


def test_escrow_survives_and_re_indexes() -> None:
    t = table(ttl=1.0)
    a = t.bind()
    obj = Obj("escrowed")
    ha = t.put(obj, a)
    check("detach moves one lease", t.detach(a, ha) == 1)
    t.audit()
    t.connections[a].last_seen -= 10.0
    t.sweep()
    t.audit()
    check("escrow keeps the object alive with no owner", ha in t.entries)
    claimed = t.bind(a)
    t.audit()
    check("bind claims it back", t.connections[claimed].leases.get(ha) == 1)
    same = t.put(obj, claimed)
    t.audit()
    check("a claimed handle is indexed again", same == ha, (same[:8], ha[:8]))


def test_bad_producer_leaves_no_trace() -> None:
    """A put naming a producer that does not exist must change
    nothing. It used to link the parents it had already resolved to a
    child handle it then failed to register, and those parents could
    never reap: they kept a child that was not in the table."""
    t = table()
    a = t.bind()
    parent = t.put(Obj("store"), a)
    before = len(t.entries)
    try:
        t.put(Obj("drv"), a, parents=[parent, "nope"])
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError for an unknown producer")
    t.audit()
    check("the failed put registered nothing", len(t.entries) == before)
    check("the real producer kept no dangling child",
          not t.entries[parent].children, t.entries[parent].children)
    t.release(a, parent)
    t.audit()
    check("so the producer still reaps", parent not in t.entries)


def test_producer_pinning_with_a_reused_child() -> None:
    """The cascade still works when the child handle is a reused one."""
    t = table()
    a = t.bind()
    store = t.put(Obj("store"), a)
    drv_obj = Obj("drv")
    d1 = t.put(drv_obj, a, parents=[store])
    d2 = t.put(drv_obj, a, parents=[store])
    t.audit()
    check("the reused child is one handle", d1 == d2)
    check("the producer counts it once", t.entries[store].children == {d1})
    t.release(a, store)
    t.audit()
    check("the producer stays while its child lives", store in t.entries)
    t.release(a, d1)
    t.release(a, d1)
    t.audit()
    check("dropping the child cascades to the producer",
          d1 not in t.entries and store not in t.entries)


def test_audit_catches_an_invented_lease() -> None:
    """Prove the checker fails when the invariant breaks - otherwise a
    green audit says nothing."""
    t = table()
    a = t.bind()
    h = t.put(Obj("x"), a)
    t.entries[h].leases += 1
    try:
        t.audit()
    except AssertionError:
        check("audit catches a lease the holders do not have", True)
    else:
        raise AssertionError("audit passed on a broken table")


def test_anonymous_holder() -> None:
    t = table()
    obj = Obj("anon")
    h1 = t.put(obj, "")
    h2 = t.put(obj, "")
    t.audit()
    check("the anonymous holder is one connection", h1 == h2)
    check("and it is keyed by ANON", ANON in t.connections)


def test_manifest_schema_is_checked() -> None:
    """A manifest from another generator must be refused, not read.

    The server and the client learn every type, policy and rpc name
    from this file. A stale one does not fail on load - it answers
    wrong, one lookup at a time (tasks/022)."""
    from fake_library_generated._wiretypes import MANIFEST_SCHEMA, check_manifest
    from fake_library_python import grpc_pb

    check("the shipped manifest passes its own check",
          grpc_pb.load_manifest()["schema"] == MANIFEST_SCHEMA)
    bad_manifests: list[dict[str, Any]] = [
        {}, {"schema": MANIFEST_SCHEMA + 1}, {"schema": "1"}]
    for bad in bad_manifests:
        try:
            check_manifest(bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted a manifest with {bad!r}")
    check("a manifest from another generator is refused", True)


def main() -> None:
    tests: list[Any] = [
        test_identity_mapping,
        test_distinct_objects_get_distinct_handles,
        test_identity_is_per_connection,
        test_index_does_not_outlive_the_entry,
        test_dead_connection_clears_its_index,
        test_share_indexes_the_target,
        test_transfer_leaves_the_source_indexed,
        test_escrow_survives_and_re_indexes,
        test_bad_producer_leaves_no_trace,
        test_producer_pinning_with_a_reused_child,
        test_audit_catches_an_invented_lease,
        test_anonymous_holder,
        test_manifest_schema_is_checked,
    ]
    for fn in tests:
        print(f"\n-- {fn.__name__}")
        fn()
    print("\nALL HANDLE TABLE CHECKS PASSED")


if __name__ == "__main__":
    main()
