"""
HandleTable unit tests (tasks/002, tasks/031).

The lifecycle suite drives the table through a real gRPC server, which
is what proves the transport. It cannot reach the cases that only the
table can produce: the same object handed out twice, a producer handle
that does not exist, an object index that must not outlive its entry.

Every step calls audit(). That method held the one invariant the whole
model rests on and nothing called it, so a lease created or destroyed
by accident stayed invisible until a handle leaked much later.

Run:  nix develop --file . shell --command pytest huggorm/tests
"""

from typing import Any

import pytest

from huggorm.lifecycle import ANON, HandleTable


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
    assert h1 == h2, f"same object, same connection: one handle :: {h1[:8]}"
    assert t.entries[h1].leases == 2, "second put adds a lease"
    assert len(t.entries) == 1, f"one entry, not two :: {len(t.entries)}"

    # Releasing once must NOT drop it: the second put took a lease.
    t.release(a, h1)
    t.audit()
    assert h1 in t.entries, "one release leaves the object alive"
    t.release(a, h1)
    t.audit()
    assert h1 not in t.entries, "the matching release drops it"


def test_distinct_objects_get_distinct_handles() -> None:
    t = table()
    a = t.bind()
    h1 = t.put(Obj("one"), a)
    h2 = t.put(Obj("two"), a)
    t.audit()
    assert h1 != h2, "distinct objects: distinct handles"


def test_identity_is_per_connection() -> None:
    t = table()
    a, b = t.bind(), t.bind()
    obj = Obj("shared")
    ha = t.put(obj, a)
    hb = t.put(obj, b)
    t.audit()
    assert ha != hb, "two connections get two handles for one object"
    assert t.get(ha) is t.get(hb) is obj, "both handles resolve to the same object"
    t.release(a, ha)
    t.audit()
    assert (
        ha not in t.entries and hb in t.entries
    ), "one connection releasing does not drop the other's handle"


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
    assert not t._by_obj.get(a), "dropped entry clears the object index"
    h2 = t.put(obj, a)
    t.audit()
    assert h2 != h1, "the same object after a drop gets a fresh handle"
    assert t.get(h2) is obj, "and it works"


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
    assert ha in dropped, f"the silent connection's handle drops :: {dropped}"
    assert hb in t.entries, "the live connection keeps its own"
    assert a not in t._by_obj, "the dead connection's index bucket is gone"
    assert a not in t.entries[hb].tokens, "the survivor's entry forgot the dead token"


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
    assert hb == ha, "put after share reuses the shared handle"
    assert (
        len(t.entries) == 1 and t.entries[ha].leases == 3
    ), f"and it took a lease rather than a second handle :: {t.entries[ha].leases}"


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
    assert ha not in t.connections[a].leases, "transfer left the source holding nothing"
    again = t.put(obj, a)
    t.audit()
    assert again == ha, "the source can take a new lease on the same handle"
    assert t.entries[ha].leases == 2, "and the entry counts both holders"


def test_escrow_survives_and_re_indexes() -> None:
    t = table(ttl=1.0)
    a = t.bind()
    obj = Obj("escrowed")
    ha = t.put(obj, a)
    assert t.detach(a, ha) == 1, "detach moves one lease"
    t.audit()
    t.connections[a].last_seen -= 10.0
    t.sweep()
    t.audit()
    assert ha in t.entries, "escrow keeps the object alive with no owner"
    claimed = t.bind(a)
    t.audit()
    assert t.connections[claimed].leases.get(ha) == 1, "bind claims it back"
    same = t.put(obj, claimed)
    t.audit()
    assert same == ha, f"a claimed handle is indexed again :: {(same[:8], ha[:8])}"


def test_bad_producer_leaves_no_trace() -> None:
    """A put naming a producer that does not exist must change
    nothing. It used to link the parents it had already resolved to a
    child handle it then failed to register, and those parents could
    never reap: they kept a child that was not in the table."""
    t = table()
    a = t.bind()
    parent = t.put(Obj("store"), a)
    before = len(t.entries)
    with pytest.raises(KeyError):
        t.put(Obj("drv"), a, parents=[parent, "nope"])
    t.audit()
    assert len(t.entries) == before, "the failed put registered nothing"
    assert (
        not t.entries[parent].children
    ), f"the real producer kept no dangling child :: {t.entries[parent].children}"
    t.release(a, parent)
    t.audit()
    assert parent not in t.entries, "so the producer still reaps"


def test_producer_pinning_with_a_reused_child() -> None:
    """The cascade still works when the child handle is a reused one."""
    t = table()
    a = t.bind()
    store = t.put(Obj("store"), a)
    drv_obj = Obj("drv")
    d1 = t.put(drv_obj, a, parents=[store])
    d2 = t.put(drv_obj, a, parents=[store])
    t.audit()
    assert d1 == d2, "the reused child is one handle"
    assert t.entries[store].children == {d1}, "the producer counts it once"
    t.release(a, store)
    t.audit()
    assert store in t.entries, "the producer stays while its child lives"
    t.release(a, d1)
    t.release(a, d1)
    t.audit()
    assert (
        d1 not in t.entries and store not in t.entries
    ), "dropping the child cascades to the producer"


def test_audit_catches_an_invented_lease() -> None:
    """Prove the checker fails when the invariant breaks - otherwise a
    green audit says nothing."""
    t = table()
    a = t.bind()
    h = t.put(Obj("x"), a)
    t.entries[h].leases += 1
    with pytest.raises(AssertionError, match="invariant violated"):
        t.audit()


def test_anonymous_holder() -> None:
    t = table()
    obj = Obj("anon")
    h1 = t.put(obj, "")
    h2 = t.put(obj, "")
    t.audit()
    assert h1 == h2, "the anonymous holder is one connection"
    assert ANON in t.connections, "and it is keyed by ANON"


def test_touch_grants_once() -> None:
    """Naming a handle makes you a holder, and naming it again does
    not make you two."""
    t = table()
    a, b = t.bind(), t.bind()
    obj = Obj("shared out of band")
    ha = t.put(obj, a)

    assert t.touch(b, ha) is True, "the borrower is granted on first use"
    t.audit()
    assert t.touch(b, ha) is False, "and not on the second"
    assert not any(t.touch(b, ha) for _ in range(10)), "or the tenth"
    t.audit()
    assert t.connections[b].leases[ha] == 1, "so it owes exactly one release"
    assert t.touch(a, ha) is False, "the acquirer's own use grants nothing"
    t.audit()

    t.release(a, ha)
    t.audit()
    assert ha in t.entries, "the object outlives its acquirer"
    t.release(b, ha)
    t.audit()
    assert ha not in t.entries, "and drops when the borrower lets go"


def test_touch_refuses_an_unknown_handle() -> None:
    """A dropped or forged id must not create anything. Granting on a
    name alone would let a caller mint holders for handles that never
    existed."""
    t = table()
    a = t.bind()
    with pytest.raises(KeyError):
        t.touch(a, "0" * 32)
    t.audit()
    assert not t.connections[a].leases, "an unknown handle grants nothing"


def test_touch_takes_ownership_back_after_detach() -> None:
    """Detach says "I am done owning this". Calling through it says
    otherwise, so the lease comes back out of escrow's shadow as a
    fresh one."""
    t = table()
    a = t.bind()
    obj = Obj("detached then used")
    ha = t.put(obj, a)
    t.detach(a, ha)
    t.audit()
    assert ha not in t.connections[a].leases, "detach left the connection holding nothing"
    assert t.touch(a, ha) is True, "calling through it grants again"
    t.audit()
    assert (
        t.escrow[a][ha] == 1 and t.entries[ha].leases == 2
    ), "escrow still holds the detached lease"


def test_manifest_schema_is_checked() -> None:
    """A manifest from another generator must be refused, not read.

    The server and the client learn every type, policy and rpc name
    from this file. A stale one does not fail on load - it answers
    wrong, one lookup at a time (tasks/022)."""
    from huggorm import grpc_pb
    from huggorm_generated._wiretypes import MANIFEST_SCHEMA, check_manifest

    assert (
        grpc_pb.load_manifest()["schema"] == MANIFEST_SCHEMA
    ), "the shipped manifest passes its own check"
    bad_manifests: list[dict[str, Any]] = [
        {}, {"schema": MANIFEST_SCHEMA + 1}, {"schema": "1"}]
    for bad in bad_manifests:
        with pytest.raises(ValueError):
            check_manifest(bad)
