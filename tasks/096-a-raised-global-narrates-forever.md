# A raised global narrates forever

**OPEN.** From `tasks/095`, which measured it. This is the fix.

## The defect

A process that has once called `subscribe_logs` or
`subscribe_process_logs` with a level above lvlInfo prints the
DAEMON's debug lines on stderr for the rest of its life - to every
caller, subscribed or not, on every thread.

`tasks/095` counted 1052 lines on an unsubscribed caller, from 1051
worker ops, deterministically across 12 repetitions.

## Why no gate catches it

The level is erased on the wire.
`worker-protocol-connection.cc:75` reads a `STDERR_NEXT` frame and
calls `printError` on its text. A daemon `debug()` therefore reaches
the client as **lvlError**, which is 0 and passes every gate this
repository has and every gate nix has. `tasks/095` holds the full
chain.

So this cannot be fixed downstream. Nothing downstream still knows
what the level was.

## The fix

Lower `nix::verbosity` when the subscription that raised it goes.

**Not to lvlInfo.** To the WIDEST level any LIVE subscription still
needs, or lvlInfo when there is none. Lowering to lvlInfo would
silently stop a still-subscribed thread's records, which is a silent
skip and this repository's named failure mode.

`packages/huggorm/huggorm/server.py` already computes exactly this
one layer up, in `_widest(readers)`, and re-opens a subscription when
a joiner wants more than the current one. The C++ needs the same
rule over live subscriptions: a count per level, or a max over a
registry - the shape is a decision, the rule is not.

## What lowering cannot reach

**A connection already open keeps the level it was given.**
`setOptions` runs once, at handshake. So a `Store` opened while a
vomit subscription was live goes on receiving vomit from its daemon
until it is closed, whatever the global says afterwards.

That is a bounded remainder and it should be written down rather than
hidden: lowering fixes every connection opened after it, which is the
common case (subscribe, work, unsubscribe, later open a store).

Whether to go further - reconnect, or refuse to raise once a
connection is open - is a separate question and probably not worth
it. Say what the limit is.

## The gate

Prove it by breaking it. The gate has to be LIVE, over a daemon
store, because `dummy://` opens no connection and reaches none of
this - which is exactly the blindness that let 089's pin regression
through and that 095 had to leave the suite to find.

A gate that counts lines on a captured stderr after a
subscribe/unsubscribe cycle fails today with 1052 and should pass
with 0. A second gate has to hold the other side: a thread still
subscribed at a raised level keeps receiving, while another thread
unsubscribes.

Opened 2026-09-05, from `tasks/095`.
