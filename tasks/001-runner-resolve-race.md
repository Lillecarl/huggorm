# Runner lazy-construction race

Review finding 1 (HIGH, proven). `BaseRunner._resolve` checks
`self._obj is None` without a lock; concurrent first-calls ran the
factory 4 times out of 8 probes. Pool-wrapped handles then diverge
(each CLocalStore has a private valid_ set).

Fix: per-runner threading.Lock around _resolve, or eager construction
when the server inserts into the handle table. Unblocks honest
concurrency tests for 002/006.
