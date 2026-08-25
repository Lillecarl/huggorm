# Track every handle that crosses, however deeply nested

Carl, 2026-08-25: "As soon as the server sends or receives a valid
handle to a client it should be lifetime tracked against that client,
preferably entirely automatic by scanning inbound and outgoing
messages recursively."

## What already happens

Outbound is already automatic, one level deep. `codec.encode` calls
the `proxy_id` callback for every proxy-typed field, and the server
passes a callback that mints a handle and leases it to the caller's
connection:

    self.codec.encode(resp, "result", m["return_type"], result,
        lambda obj: self.put(obj, _tok(stream), parents=[req.self.id]))

Inbound resolves but does not grant, which is right: the client already
holds the lease for a handle it is able to name.

## The real gap: nesting

`value_to_msg` recurses into nested wire-VALUES and nothing else:

    if self.kind(ftype) == "value":
        self.value_to_msg(ftype, val, getattr(msg, fname))
    else:
        setattr(msg, fname, val)          # a proxy lands here and breaks

`check_wire_contract` does not forbid a `_wire_fields` entry naming a
proxy type, so declaring one is accepted and then fails at the first
call that touches it. Broken by omission, not by design.

Nothing declares one today, which is why nobody has hit it. 030 makes
it certain: an attribute set carrying store paths or values is exactly
a collection with proxies inside.

## Opinion: yes outbound, no inbound, and not by scanning

**Outbound recursion: unreservedly.** It is a bug fix. Extend the
codec's existing walk so a proxy anywhere in a message gets minted and
leased the same way a top-level one does.

**Walk the declared shape, not the message.** A generic descriptor
scan for `Handle` fields would work without the manifest, but it is
less precise - it cannot tell a live proxy from a Handle used as an
opaque id in some future control message - and it puts a second thing
in this codebase that knows what a handle is. The codec already walks
the declared shape and already knows which fields are proxies.

**Inbound auto-granting: no.** lifecycle.py draws a deliberate line -
"The handle ID remains the access capability; the token decides
LIFETIME." Granting on receipt blurs it twice over:

- a client passing back a handle it already holds gets a SECOND lease
  and then owes two releases. This repo has had exactly one
  lease-inflation bug (024) and it stayed green for a long time,
  because nothing released after claiming;
- a client passing a handle it does not hold becomes an owner by using
  it. That may be desirable - it is close to what share(copy) does -
  but it is a semantics decision, not a mechanical one.

Either way the lease count becomes a function of how many times an
object was passed as an argument, which a caller cannot reason about.

## The scaling problem the two ideas create together

`put()` mints a fresh handle per call, so the same object returned
twice is two handles with two leases. Harmless today. With recursive
tracking and nested collections it stops being harmless: one Value
appearing in a hundred map entries becomes a hundred handles and a
hundred leases, and the client's finalizers then have to release a
hundred times to free one object.

Identity-mapping - one handle per object per connection, refcounted -
is the fix, and it wants doing BEFORE nested proxies land rather than
after. It also simplifies the client: 028's per-handle refcount exists
partly because the server can hand out several handles for one thing.

## Order

1. identity-mapping in HandleTable: put() returns the existing handle
   for an object this connection already holds, and bumps its lease.
2. recursive proxy encode/decode in the codec, driven by the declared
   shape.
3. check_wire_contract stops accepting a proxy field it cannot carry,
   until (2) lands.
