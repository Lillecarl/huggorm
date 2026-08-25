# A proxy parameter has no location-independent type

Raised by Carl (2026-08-25), while 017 was landing: "making sure the
'handle' RPC is always resolved for all methods. On the RPC client
someone should be able to store.is_valid_path(mystorepath) where
mystorepath is the proxy object to resolve server-side to its real
object."

## What already works

Probed live against a running server, all six cases:

- a proxy argument produced by the object being called;
- a proxy argument produced by a DIFFERENT remote object;
- a proxy argument declared as the abstract base, given a subclass
  handle (describe(Store) with a LocalStore and a RemoteStore);
- a proxy argument forged from a raw handle id, with no object
  identity on the client at all;
- a wire-value argument, which is Carl's literal example: StorePath is
  a value, so it crosses as a copy and needs no handle;
- construction arguments, by the same codec.

So the wire resolves a handle argument for every method. Two bugs
found while probing are fixed and covered:

- an affine wrapper could not be used as an argument until something
  had constructed it, so it depended on call order;
- closing an affine wrapper left its thread on the collector's list,
  and the next collection aborted the process.

## What does not work

The TYPE of a proxy parameter differs by location, and no single type
describes both:

    AsyncEvalState.force(v: Value | AsyncValue)   needs a local object
    RPCEvalState.force(v: RPCValue)               needs a handle

Parameters are contravariant, so an implementation must accept
everything the protocol promises. Neither one can. EvalState.force is
therefore absent from EvalStateLike, and the manifest records why in
protocol_blockers. It is the only method affected today.

Returns do not have this problem: the protocol names the protocol,
both implementations return something that satisfies it, and returns
are covariant.

## Options, none free

1. Protocol declares `v: ValueLike`; both implementations accept it
   and fail at RUNTIME when the object is from the wrong location.
   Buys a complete protocol, costs a real type error becoming a
   runtime one. It also un-does the honesty won on parameter
   annotations this session.
2. Protocol is generic over the proxy family. Python has no
   higher-kinded types, and one type variable used both co- and
   contravariantly must be invariant, which parse_expr and force
   cannot both satisfy. Rejected unless someone finds a shape that
   works.
3. Make a local wrapper able to accept a remote handle by fetching it.
   Impossible by definition for a proxy: a proxy is the thing that
   cannot be serialized.
4. Leave it recorded, as now. The protocol covers everything that IS
   location-independent, and the build names each exception.

Currently 4. Revisit when a second blocked method appears, or when a
real consumer needs to pass a proxy through a protocol-typed function.

## Also open, related

A CONSTRUCTOR taking a proxy parameter would hit the same wall from a
different side: the generated `__init__` is sync, so it cannot await
the argument's construction the way a method call now does. No
constructor takes a proxy today. A build-time check refusing one,
pointing here, would close the trap loudly.
