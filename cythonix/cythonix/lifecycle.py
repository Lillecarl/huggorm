"""
Handle lifecycle core: leased graph with connection-backed holders.

Transport-agnostic by design - grpclib, SSH, stdio or multiprocessing
shims all surface just two things: a request token and periodic pings.

Model (tasks/002):
- Holders are CONNECTIONS identified by a token. Every handle crossing
  the wire (Acquire, proxy-typed returns) grants one lease to the
  caller's connection. The handle ID remains the access capability;
  the token decides LIFETIME.
- A connection dies when it stops pinging past the TTL (transports do
  NOT need to report death - half-open TCP included). Death releases
  every lease the connection held.
- Entries reaching zero leases and zero live children DROP, cascading
  through the producer graph: a Value pins its producing EvalState,
  a Derivation pins its producing Store.
- Detach ends ownership without ending existence: leases move into
  escrow keyed by the connection token, immune to sweeping. A later
  Bind presenting that token claims everything escrowed under it -
  creators can exit entirely and their successors adopt the objects.
- Share duplicates (copy) or moves (transfer) one lease onto another
  LIVE connection - the fork-handover primitive.
- Naming a handle makes you a holder. A handle id is the access
  capability, so a connection that can name one is entitled to use it,
  and using it is what makes it an owner. That is what keeps an object
  alive for a second process that was handed the id out of band: it
  calls, it holds. The grant is idempotent - see touch().
- One handle per object per connection. Handing the same object to the
  same connection twice returns the handle it already has and adds a
  lease, instead of minting a second id. A message can carry the same
  object many times (an attribute set holding one Value under a hundred
  keys), and a handle per occurrence would make the client release a
  hundred times to free one object.
"""

import time
import uuid
from collections.abc import Callable, Iterable
from typing import Any

ANON = "\x00anon"

# The metadata key carrying the connection token on every request.
# Reference convention for all transports (grpclib, SSH, stdio shims).
TOKEN_HEADER = "x-cythonix-conn"


def _new_id() -> str:
    return uuid.uuid4().hex


class Entry:
    """One live handle: the wrapper object plus its lease bookkeeping."""

    __slots__ = ("children", "leases", "obj", "parents", "tokens")

    def __init__(self, obj: Any) -> None:
        self.obj = obj
        self.leases = 0
        self.parents: set[str] = set()
        self.children: set[str] = set()
        # Connection tokens whose object index points at this handle.
        # Kept so a drop can clear every index it appears in.
        self.tokens: set[str] = set()


class Connection:
    __slots__ = ("last_seen", "leases")

    def __init__(self) -> None:
        self.leases: dict[str, int] = {}
        self.last_seen = time.monotonic()


class HandleTable:
    """Owns entries, connections and escrow; knows nothing about gRPC."""

    def __init__(self, ttl: float | None = 120.0):
        self.ttl = ttl
        self.entries: dict[str, Entry] = {}
        self.connections: dict[str, Connection] = {}
        self.escrow: dict[str, dict[str, int]] = {}
        # token -> id(obj) -> handle. The entry holds a strong
        # reference to obj, so id() stays valid while the handle lives.
        self._by_obj: dict[str, dict[int, str]] = {}
        # Set by the transport layer: called with each wrapper object
        # as it drops, for async cleanup (runner shutdown etc).
        self.on_drop: Callable[[Any], None] | None = None

    # -- connection lifecycle -----------------------------------------
    def bind(self, claim_token: str | None = None) -> str:
        """Adopt or create a connection identity. Presenting a token
        claims everything detached under it."""
        token = claim_token or _new_id()
        conn = self.connections.get(token)
        if conn is None:
            conn = self.connections[token] = Connection()
        else:
            conn.last_seen = time.monotonic()
        # Escrowed leases were never subtracted from the entry (that is
        # what kept it alive with no owner), so adopting them MOVES the
        # lease back onto a connection - it does not create a new one.
        # Incrementing here inflated entry.leases permanently: the
        # claimer's later Release could never reach zero and the handle
        # leaked for the life of the process.
        for hid, n in self.escrow.pop(token, {}).items():
            conn.leases[hid] = conn.leases.get(hid, 0) + n
            if hid in self.entries:
                self._index(token, hid)
        return token

    def _conn_for(self, token: str) -> Connection:
        key = token or ANON
        conn = self.connections.get(key)
        if conn is None:
            conn = self.connections[key] = Connection()
        conn.last_seen = time.monotonic()
        return conn

    def _require_conn(self, token: str) -> Connection:
        conn = self.connections.get(token or ANON)
        if conn is None:
            raise KeyError(f"unknown connection {token!r} (expired or never bound)")
        conn.last_seen = time.monotonic()
        return conn

    # -- handles --------------------------------------------------------
    def put(self, obj: Any, holder_token: str,
            parents: Iterable[str] = ()) -> str:
        """Grant the holder one lease on this object, and return its
        handle. The holder gets the handle it already has for this
        object, or a new one."""
        # Resolve every producer BEFORE touching anything: a KeyError
        # halfway through used to leave earlier parents pointing at a
        # child handle that never got registered, which stopped those
        # parents from ever reaping.
        producers = []
        for p in parents:
            pe = self.entries.get(p)
            if pe is None:
                raise KeyError(f"producer handle {p!r} not found")
            producers.append((p, pe))
        key = holder_token or ANON
        hid = self._by_obj.get(key, {}).get(id(obj))
        if hid is None:
            hid = _new_id()
            self.entries[hid] = Entry(obj)
        entry = self.entries[hid]
        for p, pe in producers:
            entry.parents.add(p)
            pe.children.add(hid)
        self._grant(key, hid, 1)
        self._index(key, hid)
        return hid

    def _index(self, token: str, hid: str) -> None:
        entry = self.entries[hid]
        self._by_obj.setdefault(token, {})[id(entry.obj)] = hid
        entry.tokens.add(token)

    def _unindex(self, token: str, hid: str) -> None:
        entry = self.entries.get(hid)
        if entry is None:
            return
        bucket = self._by_obj.get(token)
        if bucket is not None and bucket.get(id(entry.obj)) == hid:
            del bucket[id(entry.obj)]
            if not bucket:
                del self._by_obj[token]
        entry.tokens.discard(token)

    def touch(self, token: str, hid: str) -> bool:
        """Make this connection a holder of a handle it named. Returns
        whether that granted a lease.

        At most ONE lease per connection per handle, however many times
        the connection names it. A counted grant would make the lease
        total a function of how often an object was passed as an
        argument, which no caller can reason about and no client can
        balance: a client releases once per client-side object, not
        once per call.

        A connection that detached this handle and then calls through
        it takes ownership back. Detach means "I am done owning this";
        calling says otherwise."""
        if hid not in self.entries:
            raise KeyError(f"unknown handle {hid[:8]}")
        key = token or ANON
        conn = self._conn_for(key)
        if conn.leases.get(hid):
            return False
        self._grant(key, hid, 1)
        # Only when this connection has no handle for the object yet.
        # Overwriting would point a later put() at the handle someone
        # else minted, in place of the one this connection was given.
        if id(self.entries[hid].obj) not in self._by_obj.get(key, {}):
            self._index(key, hid)
        return True

    def get(self, hid: str) -> Any:
        return self.entries[hid].obj

    def _grant(self, token: str, hid: str, n: int) -> None:
        conn = self._conn_for(token)
        conn.leases[hid] = conn.leases.get(hid, 0) + n
        self.entries[hid].leases += n

    def release(self, token: str, hid: str, n: int = 1) -> None:
        conn = self._require_conn(token)
        held = conn.leases.get(hid, 0)
        if held < n:
            raise ValueError(
                f"connection does not hold {n} lease(s) on {hid[:8]} (has {held})")
        if held == n:
            del conn.leases[hid]
        else:
            conn.leases[hid] = held - n
        self.entries[hid].leases -= n
        self._reap()

    def share(self, from_token: str, to_token: str, hid: str, mode: str = "copy") -> None:
        """Duplicate or move one lease onto another LIVE connection."""
        src = self._require_conn(from_token)
        if to_token == (from_token or ANON):
            raise ValueError("share target must differ from the granting connection")
        dst = self.connections.get(to_token)
        if dst is None:
            raise KeyError(f"share target {to_token!r} has not bound")
        held = src.leases.get(hid, 0)
        if held < 1:
            raise ValueError(f"granting connection does not hold {hid[:8]}")
        if mode == "transfer":
            if held == 1:
                del src.leases[hid]
            else:
                src.leases[hid] = held - 1
            # Net entry count unchanged: the lease moved, not multiplied.
        elif mode == "copy":
            self.entries[hid].leases += 1
        else:
            raise ValueError(f"unknown share mode {mode!r} (copy|transfer)")
        dst.leases[hid] = dst.leases.get(hid, 0) + 1
        # The target now holds this object under this handle, so a
        # later put() of the same object must reuse it rather than
        # mint a second one.
        self._index(to_token, hid)

    def detach(self, token: str, hid: str | None = None) -> int:
        """Move this connection's lease(s) into escrow. Returns the
        number of leases detached. Escrow keeps objects alive with no
        owner until someone Binds with our token."""
        conn = self._require_conn(token)
        targets = [hid] if hid is not None else list(conn.leases)
        moved = 0
        for t in targets:
            n = conn.leases.pop(t, 0)
            if n:
                bucket = self.escrow.setdefault(token or ANON, {})
                bucket[t] = bucket.get(t, 0) + n
                moved += n
        return moved

    # -- invariants -------------------------------------------------------
    def audit(self) -> None:
        """Check the one invariant the whole model rests on: an entry's
        lease count equals what connections hold plus what sits in
        escrow. Every mutation is a MOVE between those three places, so
        a mismatch means a lease was created or destroyed by accident -
        which shows up much later as a leaked or prematurely reaped
        handle. Cheap enough for tests to call after every step."""
        counted: dict[str, int] = {}
        for conn in self.connections.values():
            for hid, n in conn.leases.items():
                counted[hid] = counted.get(hid, 0) + n
        for bucket in self.escrow.values():
            for hid, n in bucket.items():
                counted[hid] = counted.get(hid, 0) + n
        bad = []
        for hid, entry in self.entries.items():
            held = counted.pop(hid, 0)
            if held != entry.leases:
                bad.append(f"{hid[:8]}: entry says {entry.leases}, holders say {held}")
        for hid, n in counted.items():
            bad.append(f"{hid[:8]}: {n} lease(s) held on a dropped entry")
        if bad:
            raise AssertionError("handle table invariant violated: " + "; ".join(bad))

    # -- reaping ----------------------------------------------------------
    def sweep(self, now: float | None = None) -> list[str]:
        """Release every lease held by connections silent past the TTL,
        then cascade-drop. Returns dropped handle IDs.

        Escrow is deliberately UNTOUCHED: detached leases are unowned
        and never auto-reaped - that is what lets a creator exit
        entirely while its objects wait for a claim."""
        if self.ttl:
            now = now if now is not None else time.monotonic()
            dead = [t for t, c in self.connections.items() if now - c.last_seen > self.ttl]
            for t in dead:
                conn = self.connections.pop(t)
                for hid, n in conn.leases.items():
                    self.entries[hid].leases -= n
                for hid in self._by_obj.pop(t, {}).values():
                    entry = self.entries.get(hid)
                    if entry is not None:
                        entry.tokens.discard(t)
        return self._reap()

    def _reap(self) -> list[str]:
        """Fixpoint: drop entries with no leases and no live children;
        unlinking children may free their parents in turn."""
        dropped: list[str] = []
        progress = True
        while progress:
            progress = False
            for hid, entry in list(self.entries.items()):
                if entry.leases == 0 and not entry.children:
                    for t in list(entry.tokens):
                        self._unindex(t, hid)
                    del self.entries[hid]
                    dropped.append(hid)
                    for p in entry.parents:
                        pe = self.entries.get(p)
                        if pe is not None:
                            pe.children.discard(hid)
                    if self.on_drop is not None:
                        self.on_drop(entry.obj)
                    progress = True
        return dropped
