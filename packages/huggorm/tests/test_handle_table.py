"""The lease invariant, under random sequences of every operation.

`HandleTable.audit` checks that an entry's lease count equals what
connections hold plus what escrow holds. Every mutation is a MOVE
between those places, so a mismatch is a lease made or lost by
accident - a leaked handle, or one reaped under its holder. The
escrow double count in `bind` was that bug, and an ad-hoc randomized
loop is what would have caught it first (huggorm#12). This is that
loop, committed: fixed seeds, so a failure names the seed and the
step that broke it.

A refusal is part of the sequence. A KeyError or ValueError is the
table declining a bad request, and the table must be consistent
afterwards all the same.
"""

import random

import pytest

from huggorm.lifecycle import HandleTable, ShareMode

SEEDS = range(64)
STEPS = 300


def _step(table: HandleTable, rng: random.Random) -> str:
    tokens = list(table.connections)
    hids = list(table.entries)
    claimable = list(table.escrow)
    op = rng.choice(["bind", "claim", "put", "put", "put", "release",
                     "release", "share", "detach", "die"])
    if op == "bind" or not tokens:
        table.bind()
        return "bind"
    token = rng.choice(tokens)
    if op == "claim" and claimable:
        table.bind(rng.choice(claimable))
        return "claim"
    if op == "put" or not hids:
        reuse = hids and rng.random() < 0.3
        obj = table.get(rng.choice(hids)) if reuse else object()
        parents = rng.sample(hids, k=min(len(hids), rng.randint(0, 2)))
        table.put(obj, token, parents=parents)
        return "put"
    hid = rng.choice(hids)
    if op == "release":
        table.release(token, hid)
    elif op == "share":
        table.share(token, rng.choice(tokens), hid,
                    mode=rng.choice(list(ShareMode)))
    elif op == "detach":
        table.detach(token, hid if rng.random() < 0.5 else None)
    else:
        table.connections[token].last_seen -= 10 * (table.ttl or 1)
        table.sweep()
    return op


@pytest.mark.parametrize("seed", SEEDS)
def test_every_operation_keeps_the_lease_count(seed: int) -> None:
    rng = random.Random(seed)
    table = HandleTable(ttl=60.0)
    for step in range(STEPS):
        try:
            op = _step(table, rng)
        except (KeyError, ValueError) as refused:
            op = f"refused: {refused}"
        try:
            table.audit()
        except AssertionError as broken:
            raise AssertionError(
                f"seed {seed}, step {step}, after {op}: {broken}") from None


def test_the_sequences_reach_every_operation() -> None:
    """Non-vacuity: the seeds above drive each operation, and some of
    them on a table holding escrow and parents."""
    seen: set[str] = set()
    for seed in SEEDS:
        rng = random.Random(seed)
        table = HandleTable(ttl=60.0)
        for _ in range(STEPS):
            try:
                seen.add(_step(table, rng))
            except (KeyError, ValueError):
                seen.add("refused")
    assert {"bind", "claim", "put", "release", "share", "detach", "die",
            "refused"} <= seen, seen
