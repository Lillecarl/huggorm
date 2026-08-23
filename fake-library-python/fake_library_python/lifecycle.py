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
"""

import time
import uuid

ANON = "\x00anon"

# The metadata key carrying the connection token on every request.
# Reference convention for all transports (grpclib, SSH, stdio shims).
TOKEN_HEADER = "x-nixmock-conn"


def _new_id() -> str:
    return uuid.uuid4().hex


class Entry:
    """One live handle: the wrapper object plus its lease bookkeeping."""

    __slots__ = ("obj", "leases", "parents", "children")

    def __init__(self, obj):
        self.obj = obj
        self.leases = 0
        self.parents: set[str] = set()
        self.children: set[str] = set()


class Connection:
    __slots__ = ("leases", "last_seen")

    def __init__(self):
        self.leases: dict[str, int] = {}
        self.last_seen = time.monotonic()


class HandleTable:
    """Owns entries, connections and escrow; knows nothing about gRPC."""

    def __init__(self, ttl: float | None = 120.0):
        self.ttl = ttl
        self.entries: dict[str, Entry] = {}
        self.connections: dict[str, Connection] = {}
        self.escrow: dict[str, dict[str, int]] = {}
        # Set by the transport layer: called with each wrapper object
        # as it drops, for async cleanup (runner shutdown etc).
        self.on_drop = None

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
        for hid, n in self.escrow.pop(token, {}).items():
            conn.leases[hid] = conn.leases.get(hid, 0) + n
            self.entries[hid].leases += n
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
    def put(self, obj, holder_token: str, parents=()) -> str:
        """Register a wrapper and grant one lease to the holder."""
        hid = _new_id()
        entry = Entry(obj)
        for p in parents:
            pe = self.entries.get(p)
            if pe is None:
                raise KeyError(f"producer handle {p!r} not found")
            entry.parents.add(p)
            pe.children.add(hid)
        self.entries[hid] = entry
        self._grant(holder_token, hid, 1)
        return hid

    def get(self, hid: str):
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
